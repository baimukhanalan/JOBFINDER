"""Re-verify and REVIVE `job_catalog` rows that the OLD bare-`no_form` bug wrongly blacklisted.

Before 2026-09-13, a co-pilot fill that returned `no_form` (0 fields, no form found) marked the
catalog row `dead` UNCONDITIONALLY — but a `no_form` on a SLOW / flapping egress render (the React
form's async fetch didn't finish inside the field-poll window) is NOT proof of death. ~400 live
postings were killed this way (dead_reason "posting gone (404 / not found at source)" from the bulk
drain + "campaign: no form at the ATS (posting gone)" from the campaign cron, plus a couple of
same-family sweep reasons). The 2026-09-13 fix gates mark_dead on a POSITIVELY-classified terminal
page_type, so NEW kills are safe — but the already-killed rows persist (the nightly collector's
upsert refreshes last_seen but does NOT reset `dead`), so they stay hidden from list_jobs /
jobs_for_drafting and never re-enter the apply pool.

This is a one-shot, conservative, idempotent cleanup. It does a LIGHT liveness re-check — NO fill,
NO browser — by fetching each posting's BOARD ONCE from the ATS public JSON API (the same endpoints
the nightly collector uses) and checking whether the posting id is still on the board:

  * board says the posting id is STILL live      -> REVIVE (dead=FALSE, dead_reason=NULL)
  * board fetched OK but the id is GONE           -> keep dead (genuinely removed at the source)
  * board API 404s (whole company board gone)     -> keep dead
  * board fetch failed (timeout/5xx/rate-limit)   -> keep dead, UNLESS the nightly collector re-saw
                                                     the row within --fresh-days (positive proof of
                                                     life) -> REVIVE

Reviving is safe/self-correcting: a genuinely-gone posting that slips through is re-marked dead by
the (now-gated) fill logic on its next real load. Only POSITIVE evidence of life ever revives a row;
absence of evidence keeps it dead. One request per distinct board, paced + backed-off, so no host is
hammered.

    PYTHONPATH=. python3 -m backend.tools.revive_dead_catalog            # DRY-RUN (default)
    PYTHONPATH=. python3 -m backend.tools.revive_dead_catalog --apply    # actually revive
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
import time
from collections import Counter, defaultdict

import httpx

from backend.tools import catalog_db

# The dead_reason values written by the two no_form code paths (+ same-family manual sweeps) —
# the "uncertain" kills. Explicitly NOT included: "stale" (reliable board-diff), "blocklist-gone"
# (discovery blocklist), "greenhouse 404 (posting removed at source)" (a POSITIVE HTTP-404 sweep).
NOFORM_REASONS = (
    "posting gone (404 / not found at source)",
    "campaign: no form at the ATS (posting gone)",
    "no_form: expired posting (sweep 2026-09-13)",
    "campaign: no form at the ASS (posting gone, Ashby «Job not found» 2026-09-09)",
)

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0 Safari/537.36"}
_GH_ID = re.compile(r"(?:/jobs/|[?&]gh_jid=)(\d+)")

# status codes for a board fetch
OK, GONE_404, ERROR = "ok", "http_404", "error"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def match_key(row: dict) -> str:
    """The id to compare against a fresh board's live-id set. Greenhouse keys on the numeric gh_jid
    (external_id if numeric, else pulled from the stored url — external_id can be a collector sha1
    fallback); every other ATS keys on the stable external_id (e.g. Ashby's posting UUID)."""
    ats = row.get("ats")
    ext = str(row.get("external_id") or "").strip()
    if ats == "greenhouse":
        if ext.isdigit():
            return ext
        m = _GH_ID.search(row.get("url") or "")
        if m:
            return m.group(1)
    return ext


def _board_ids(ats: str, slug: str, client: httpx.Client) -> tuple[str, set[str]]:
    """(status, live_id_set) for one board via its ATS public JSON API. Mirrors applier/boards.py
    endpoints; ids are str(job['id']) so they match catalog_collector's external_id derivation."""
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json",
        "workable": f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
        "breezy": f"https://{slug}.breezy.hr/json",
    }
    url = urls.get(ats)
    if not url:
        return ERROR, set()
    r = client.get(url, headers=_UA)
    if r.status_code == 404:
        return GONE_404, set()
    if r.status_code != 200:
        return ERROR, set()
    try:
        data = r.json()
    except Exception:
        return ERROR, set()
    if ats == "lever":
        jobs = data if isinstance(data, list) else []
    elif ats == "breezy":
        jobs = data if isinstance(data, list) else []
    else:
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
    ids = {str(j.get("id")) for j in jobs if isinstance(j, dict) and j.get("id") is not None}
    return OK, ids


def fetch_board(ats: str, slug: str, client: httpx.Client, retries: int = 3,
                base_delay: float = 1.0) -> tuple[str, set[str]]:
    """fetch a board with exponential backoff (delay = min(base*2^n, 30s)); a 404 is terminal
    (never retried), a transient error is retried, per the project's reconnect-backoff rule."""
    delay = base_delay
    for attempt in range(1, retries + 1):
        try:
            status, ids = _board_ids(ats, slug, client)
            if status in (OK, GONE_404):
                return status, ids
        except Exception:
            status = ERROR
        if attempt < retries:
            time.sleep(min(delay, 30.0))
            delay *= 2
    return ERROR, set()


def classify(mkey: str, status: str, live_ids: set[str], last_seen, now: datetime.datetime,
             fresh_days: int) -> tuple[str, str]:
    """PURE verdict for one row. Returns (verdict, detail) with verdict in {'live','dead'}.

    Only POSITIVE evidence of life revives: the posting id is on the freshly-fetched board, OR the
    board fetch failed but the nightly collector re-saw this row within `fresh_days` (its upsert
    refreshes last_seen even on dead rows). A gone id, a 404 board, or an unverifiable stale row
    stays dead."""
    if status == OK:
        if mkey and mkey in live_ids:
            return "live", "on_board"
        return "dead", "gone_from_board"
    if status == GONE_404:
        return "dead", "board_404"
    # status == ERROR: fall back to the collector's own recent re-sighting as proof of life.
    if last_seen is not None:
        ls = last_seen
        if ls.tzinfo is None:
            ls = ls.replace(tzinfo=datetime.timezone.utc)
        if ls >= now - datetime.timedelta(days=fresh_days):
            return "live", "recent_collect"
    return "dead", "board_error"


def run(reasons=NOFORM_REASONS, apply: bool = False, delay: float = 0.7, fresh_days: int = 2,
        timeout: float = 25.0, limit_boards: int | None = None) -> dict:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = catalog_db.dead_rows_by_reason(list(reasons))
    print(f"{ts} revive_dead_catalog: {len(rows)} dead candidate rows "
          f"({'APPLY' if apply else 'DRY-RUN'}) across reasons={list(reasons)}", flush=True)
    if not rows:
        return {"candidates": 0, "revived": 0, "kept_dead": 0}

    by_board: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_board[(r["ats"], r["company_key"])].append(r)
    boards = sorted(by_board)
    if limit_boards:
        boards = boards[:limit_boards]
    print(f"{ts} probing {len(boards)} distinct boards (1 request each, paced {delay}s)", flush=True)

    now = _now()
    revive_keys: list[tuple] = []
    detail_counts: Counter = Counter()
    board_status: Counter = Counter()
    revived_by_ats: Counter = Counter()

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for i, (ats, slug) in enumerate(boards, 1):
            status, live_ids = fetch_board(ats, slug, client)
            board_status[status] += 1
            group = by_board[(ats, slug)]
            live_here = 0
            for r in group:
                verdict, detail = classify(match_key(r), status, live_ids, r.get("last_seen"),
                                           now, fresh_days)
                detail_counts[detail] += 1
                if verdict == "live":
                    revive_keys.append((r["ats"], r["company_key"], r["external_id"]))
                    revived_by_ats[ats] += 1
                    live_here += 1
            print(f"  [{i}/{len(boards)}] {ats}/{slug}: {status} "
                  f"(board_jobs={len(live_ids)}, rows={len(group)}, revive={live_here})", flush=True)
            if i < len(boards):
                time.sleep(delay)

    revived = 0
    if apply and revive_keys:
        revived = catalog_db.revive(revive_keys)
    elif revive_keys:
        revived = len(revive_keys)  # dry-run: report what WOULD be revived

    kept = len(rows) - len(revive_keys)
    print("", flush=True)
    print(f"{ts} ===== SUMMARY =====", flush=True)
    print(f"  candidates      : {len(rows)}", flush=True)
    print(f"  {'REVIVED' if apply else 'would revive'}    : {revived}  "
          f"(by ats: {dict(revived_by_ats)})", flush=True)
    print(f"  kept dead       : {kept}", flush=True)
    print(f"  verdict detail  : {dict(detail_counts)}", flush=True)
    print(f"  board fetches   : {dict(board_status)} (of {len(boards)})", flush=True)
    if not apply:
        print("  (DRY-RUN — pass --apply to write dead=FALSE for the revived rows)", flush=True)
    return {"candidates": len(rows), "revived": revived, "kept_dead": kept,
            "detail": dict(detail_counts), "board_status": dict(board_status)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-verify + revive wrongly-dead job_catalog rows")
    ap.add_argument("--apply", action="store_true",
                    help="write dead=FALSE for verified-live rows (default: dry-run)")
    ap.add_argument("--reason", action="append", dest="reasons", default=None,
                    help="override the target dead_reason(s); repeatable (default: the no_form set)")
    ap.add_argument("--fresh-days", type=int, default=2,
                    help="a board-fetch failure still revives a row the collector re-saw within N "
                         "days (default 2 ~= the last nightly collect)")
    ap.add_argument("--delay", type=float, default=0.7,
                    help="seconds to pace between board fetches (default 0.7)")
    ap.add_argument("--timeout", type=float, default=25.0, help="per-request timeout (default 25s)")
    ap.add_argument("--limit-boards", type=int, default=None,
                    help="probe only the first N boards (for a smoke test)")
    a = ap.parse_args()
    reasons = tuple(a.reasons) if a.reasons else NOFORM_REASONS
    try:
        run(reasons=reasons, apply=a.apply, delay=a.delay, fresh_days=a.fresh_days,
            timeout=a.timeout, limit_boards=a.limit_boards)
    except Exception as exc:
        print(f"revive_dead_catalog error: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
