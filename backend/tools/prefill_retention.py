"""Prune pre-fill artifacts (tailored résumé PDF, screenshots, report.json, persona.json)
older than N days so uploads/prefill/ doesn't grow forever. Default 20 days.

A job dir <candidate>/<jobid>/ is pruned by its NEWEST file's mtime (so a dir touched
recently is kept). A candidate dir left with no job dirs and no other files is removed
too. status.json (a tiny per-candidate submit overlay) is kept.

    python3 -m backend.tools.prefill_retention --days 20 [--dry-run]
"""
import argparse
import shutil
import time
from pathlib import Path

PREFILL_ROOT = Path(__file__).resolve().parents[2] / "uploads" / "prefill"


def _newest_mtime(d: Path) -> float:
    try:
        m = d.stat().st_mtime
    except OSError:
        return 0.0
    for f in d.rglob("*"):
        try:
            m = max(m, f.stat().st_mtime)
        except OSError:
            pass
    return m


def prune(days: int = 20, dry_run: bool = False) -> dict:
    cutoff = time.time() - days * 86400
    jobs = cands = 0
    if not PREFILL_ROOT.is_dir():
        return {"removed_jobs": 0, "removed_candidate_dirs": 0}
    for cand in PREFILL_ROOT.iterdir():
        if not cand.is_dir():
            continue
        for job in list(cand.iterdir()):
            if not job.is_dir():
                continue
            if _newest_mtime(job) < cutoff:
                print(f"prune job {job.relative_to(PREFILL_ROOT)}")
                if not dry_run:
                    shutil.rmtree(job, ignore_errors=True)
                jobs += 1
        # remove a candidate dir only if truly empty now (no job dirs, no loose files)
        try:
            if not any(cand.iterdir()):
                print(f"prune empty candidate {cand.name}")
                if not dry_run:
                    cand.rmdir()
                cands += 1
        except OSError:
            pass
    return {"removed_jobs": jobs, "removed_candidate_dirs": cands}


def main() -> None:
    ap = argparse.ArgumentParser(description="Prune old uploads/prefill artifacts")
    ap.add_argument("--days", type=int, default=20, help="max age in days (default 20)")
    ap.add_argument("--dry-run", action="store_true", help="report only, delete nothing")
    args = ap.parse_args()
    res = prune(days=args.days, dry_run=args.dry_run)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"{stamp} prefill_retention days={args.days} dry_run={args.dry_run} "
          f"removed_jobs={res['removed_jobs']} removed_candidate_dirs={res['removed_candidate_dirs']}")


if __name__ == "__main__":
    main()
