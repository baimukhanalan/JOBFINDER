"""Bulk-generate US / Canada test candidates and upsert them into the roster.

    python -m backend.tools.gen_na_batch --us 120 --ca 90        # 210 total (default)
    python -m backend.tools.gen_na_batch --us 50 --ca 50 --dry-run

For each candidate it writes a profile into backend/data/profiles.json and a fact
sheet into backend/data/facts/<id>.json, exactly like the other generators, so the
dashboard / batch / bot pick them up with no further wiring. Identity is American
or Canadian (names, NANP phones, US/CA cities + states + timezones, ZIP / postal
codes, annual USD/CAD salary, citizen/PR work authorization) and the résumé body is
localized to REAL North American employers + universities (per backend.tools.
gen_profiles._localize_arch) — a customer-support persona is never handed an iOS
role, and no company or school is invented. Nothing is submitted; the engine only
ever pre-fills for a human to review.

Role families are round-robined across the batch so all 10 archetype families are
represented. IDs are gen_us_NNN_… / gen_ca_NNN_… (3-digit, zero-padded → stable
lexicographic sort in the inbox roster).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from backend.applier.profile_validator import validate_profile
from backend.tools.gen_profiles import ARCHETYPES, generate

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "backend" / "data"
PROFILES_JSON = DATA_DIR / "profiles.json"
FACTS_DIR = DATA_DIR / "facts"

# Deterministic seed bases so a re-run reproduces (and cleanly replaces) the same set.
_SEED_BASE = {"us": 700_000, "ca": 800_000}
_FAMILIES = list(ARCHETYPES)  # round-robin over all 10 archetype families

# gen_us_5_first_last -> gen_us_005_first_last (zero-pad the numeric segment to 3).
_ID_NUM = re.compile(r"^(gen_[a-z]{2}_)(\d+)(_.*)$")


def _pad_id(pid: str) -> str:
    return _ID_NUM.sub(lambda m: f"{m.group(1)}{int(m.group(2)):03d}{m.group(3)}", pid)


def _load_profiles() -> list[dict]:
    if PROFILES_JSON.exists():
        return json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
    return []


def _save_profiles(items: list[dict]) -> None:
    PROFILES_JSON.write_text(json.dumps(items, indent=2, ensure_ascii=False),
                             encoding="utf-8")


def build(country: str, count: int, start: int = 1) -> list[tuple[dict, dict]]:
    """Generate `count` (profile, facts) pairs for one country, round-robining role
    families for even coverage. Raises on any reality-gate failure (should never
    happen — NANP phones + gmail addresses always pass)."""
    out: list[tuple[dict, dict]] = []
    for n in range(count):
        idx = start + n
        family = _FAMILIES[n % len(_FAMILIES)]
        profile, facts = generate(idx, family=family,
                                  seed=_SEED_BASE[country] + idx, country=country)
        profile["id"] = _pad_id(profile["id"])
        problems = validate_profile(profile)
        if problems:
            raise SystemExit(f"reality-gate FAILED for {profile['id']}: {problems}")
        out.append((profile, facts))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate US/CA test candidates.")
    ap.add_argument("--us", type=int, default=120, help="number of US candidates")
    ap.add_argument("--ca", type=int, default=90, help="number of Canada candidates")
    ap.add_argument("--dry-run", action="store_true",
                    help="generate + validate but do not write any files")
    args = ap.parse_args()

    batches = build("us", args.us) + build("ca", args.ca)
    new_ids = {p["id"] for p, _ in batches}
    if len(new_ids) != len(batches):
        raise SystemExit("internal error: duplicate ids generated")

    fam_counts: dict[str, int] = {}
    for _, f in batches:
        fam_counts[f["role_family"]] = fam_counts.get(f["role_family"], 0) + 1

    print(f"Generated {len(batches)} candidates "
          f"(US={args.us}, CA={args.ca}); all passed the reality gate.")
    print("Primary family distribution:",
          ", ".join(f"{k}={v}" for k, v in sorted(fam_counts.items())))
    for (p, _) in batches[:2] + batches[-2:]:
        print(f"  e.g. {p['id']:36s} {p['full_name']:22s} {p['location']}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    items = _load_profiles()
    items = [p for p in items if p.get("id") not in new_ids]  # replace on re-run
    items.extend(p for p, _ in batches)
    _save_profiles(items)

    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    for profile, facts in batches:
        (FACTS_DIR / f"{profile['id']}.json").write_text(
            json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nWrote {len(batches)} profiles → {PROFILES_JSON}")
    print(f"Wrote {len(batches)} fact sheets → {FACTS_DIR}/")
    print(f"Roster now holds {len(items)} profiles total.")


if __name__ == "__main__":
    main()
