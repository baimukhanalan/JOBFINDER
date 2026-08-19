"""Generate a POOL of N distinct fictional candidates (default 30).

    python -m backend.tools.gen_pool --count 30 --country kz [--ai]

Each gets a distinct ФИО + identity; résumés are tailored to a specific position
LATER, at prefill time, by the job description's keywords (services/tailor). The
same pool can then be reused across positions — every position re-tailors the résumé
to its own keywords. Writes profiles.json entries + facts/<id>.json.
"""
from __future__ import annotations

import argparse

from backend.tools.gen_profiles import generate
from backend.tools.salmon_autofill import _upsert_profile


def build_pool(count: int, country: str, use_llm: bool) -> list[dict]:
    made: list[dict] = []
    seen: set[str] = set()
    idx = 0
    # keep drawing until we have `count` distinct full names
    while len(made) < count and idx < count * 4:
        idx += 1
        pd, facts = generate(idx, seed=idx * 97 + 13, use_llm=use_llm, country=country)
        if pd["full_name"] in seen:
            continue
        seen.add(pd["full_name"])
        _upsert_profile(pd, facts)
        made.append(pd)
    return made


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--country", default="kz", choices=["kz", "ph"])
    ap.add_argument("--ai", action="store_true",
                    help="use the LLM for varied bullets/summary (slower; 1 call each)")
    a = ap.parse_args()
    pool = build_pool(a.count, a.country, a.ai)
    print(f"Generated {len(pool)} distinct candidates ({a.country}):\n")
    for i, p in enumerate(pool, 1):
        print(f"  {i:2}. {p['full_name']:26} {p['phone']:18} {p['desired_salary']}")
    print(f"\nWritten to backend/data/profiles.json (+ facts/). Résumés are tailored to "
          f"each position at prefill time by the job description's keywords.")


if __name__ == "__main__":
    main()
