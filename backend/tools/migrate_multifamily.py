"""Migrate the existing candidate pool to the MULTI-FAMILY model, in place.

One identity should honestly cover several ADJACENT role families (primary + the
`gen_profiles.ADJACENCY` neighbours) so the same person can be tailored and applied
across role types (e.g. fin-risk AND data AND analyst) without fabricating a career.

This rebuilds ONLY each persona's résumé body and the family fields in its facts
sheet. It PRESERVES identity verbatim (id / name / email / phone / city / location)
so per-application reply addresses, the inbox roster numbering, and saved sessions all
keep working. Résumé content is assembled by `gen_profiles.build_resume` from the same
real, JD-grounded archetypes — no new company or school is ever introduced.

Usage (run from repo ROOT with PYTHONPATH=.):
    python -m backend.tools.migrate_multifamily              # dry-run: validate only
    python -m backend.tools.migrate_multifamily --apply      # write backups + files
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path

from backend.profiles.store import DATA_DIR
from backend.services.tailor.ats_score import ats_score
from backend.tools.gen_profiles import (
    ADJACENCY, ARCHETYPES, build_resume, cluster_titles, cluster_tools, families_for,
)

PROFILES_FILE = DATA_DIR / "profiles.json"
FACTS_DIR = DATA_DIR / "facts"

GATE_MIN = 60.0  # runner.MATCH_GATE_MIN — a covered family must clear this on the BASE résumé


def _seed(pid: str) -> int:
    """Stable per-persona seed (process-hash-salt independent)."""
    return int(hashlib.sha1(pid.encode()).hexdigest()[:8], 16)


def _synthetic_jd(fam: str) -> tuple[str, str]:
    """A representative JD for a family, built from its own archetype requirements —
    a fair offline proxy for 'a real role in this family' when scoring the gate."""
    a = ARCHETYPES.get(fam, {})
    title = (a.get("role_titles") or a.get("prior_titles") or [fam])[0]
    core = a.get("core_skills", [])
    sf = a.get("strong_fit_keywords", [])
    reqs = "; ".join(core[:10] + sf[:8])
    body = (
        f"{title}\n\n"
        f"About the role: we are hiring a {title} for our fintech team.\n\n"
        f"Requirements: you must have proven, hands-on experience with {reqs}.\n\n"
        f"What makes you a strong fit: {'; '.join(core[:8])}.\n"
    )
    return title, body


def _fit(resume: dict, fam: str) -> int:
    title, jd = _synthetic_jd(fam)
    r = dict(resume)
    r["_jd_title"] = title
    return ats_score(jd, r)["score"]


def _stretch_family(families: list[str]) -> str | None:
    """A family NONE of the cluster covers or is adjacent to — the honest 'should be
    gated' control (e.g. mobile-dev for a risk/data/analyst persona)."""
    covered = set(families)
    for f in families:
        covered |= set(ADJACENCY.get(f, []))
    for cand in ("mobile-dev", "backend-dev", "sales-mktg", "infra-sec", "qa"):
        if cand not in covered:
            return cand
    return None


def migrate(apply: bool) -> None:
    profiles = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    rows = []
    changed_profiles = 0

    for p in profiles:
        pid = p.get("id", "")
        if p.get("is_sample") or not pid.startswith("gen_"):
            continue
        facts_path = FACTS_DIR / f"{pid}.json"
        if not facts_path.exists():
            rows.append((pid, "SKIP: no facts file", "", "", ""))
            continue
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
        primary = facts.get("role_family")
        if not primary or primary not in ARCHETYPES:
            rows.append((pid, f"SKIP: bad primary {primary!r}", "", "", ""))
            continue

        rng = random.Random(_seed(pid))
        families = families_for(primary, rng)  # same call ORDER as generate()
        try:
            years = int(str(p.get("years_experience") or "5").split()[0])
        except Exception:
            years = 5
        old_resume = p.get("resume") or {}
        pi = old_resume.get("personal_info") or {
            "full_name": p.get("full_name", ""), "email": p.get("email", ""),
            "phone": p.get("phone", ""), "location": p.get("location", "")}
        resume = build_resume(rng, families, years, pi, None)

        # Validate: every covered family must clear the gate on the BASE résumé; a true
        # stretch must NOT (that's the honesty guard working, not a bug).
        fits = {f: _fit(resume, f) for f in families}
        stretch = _stretch_family(families)
        stretch_fit = _fit(resume, stretch) if stretch else None
        ok = all(v >= GATE_MIN for v in fits.values())
        flag = "OK " if ok else "LOW"
        rows.append((pid, "→".join(families),
                     " ".join(f"{f}={v}" for f, v in fits.items()),
                     f"{stretch}={stretch_fit}" if stretch else "-",
                     flag))

        if apply:
            p["resume"] = resume
            facts["role_family"] = primary
            facts["role_families"] = families
            facts["target_titles"] = cluster_titles(families)
            facts["tools"] = cluster_tools(families)[:10]
            deg = (resume.get("education") or [{}])[0].get("degree", "")
            facts["education_level"] = ("Master's degree" if "Master" in deg
                                        else "Bachelor's degree")
            facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            changed_profiles += 1

    # Report
    print(f"{'profile':38} {'cluster':34} {'covered-family fit':46} {'stretch':18} ok")
    print("-" * 140)
    low = 0
    for pid, cluster, fits, stretch, flag in rows:
        if flag == "LOW":
            low += 1
        print(f"{pid:38} {cluster:34} {fits:46} {stretch:18} {flag}")
    print("-" * 140)
    n = sum(1 for r in rows if r[4] in ("OK ", "LOW"))
    print(f"{n} personas · {low} with a family BELOW gate({int(GATE_MIN)}) on base résumé")

    if apply:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        bak = PROFILES_FILE.with_name(f"profiles.json.bak-premultifam-{ts}")
        shutil.copy2(PROFILES_FILE, bak)
        facts_bak = FACTS_DIR.with_name(f"facts.bak-premultifam-{ts}")
        if not facts_bak.exists():
            shutil.copytree(FACTS_DIR, facts_bak)
        PROFILES_FILE.write_text(json.dumps(profiles, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print(f"APPLIED: rewrote {changed_profiles} résumés + facts.")
        print(f"  backup: {bak.name} , {facts_bak.name}")
    else:
        print("DRY-RUN: nothing written. Re-run with --apply to persist.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write backups + migrated files")
    migrate(ap.parse_args().apply)
