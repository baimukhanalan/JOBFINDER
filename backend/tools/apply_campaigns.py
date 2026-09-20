"""Recurring apply-campaign store: apply DAILY under a fixed custom name, N times/day, each run
with a FRESH résumé. A campaign targets one job (`job`), a search query (`search`, e.g.
"Kazakhstan" → the region-eligible jobs), or a SET of catalog jobs the owner hand-picked on
/catalog (`jobs`). Driven by `apply_campaign_cron.py`.

Store: backend/data/apply_campaigns.json (gitignored), atomic-write + RLock (mirrors mh_settings).

SEMANTICS (owner-approved 2026-09-09):
  * SEARCH campaign: `per_day` = N distinct NEW jobs/day (never re-applies a job it already did or one
    already submitted globally), and only auto-submittable ATSes (`_AUTO_ATS`).
  * SINGLE-job campaign: `per_day` applications/day to that ONE posting (owner-requested — same name,
    fresh résumé each) under ONE STABLE mailbox (`email_mode='fixed'`), so an ATS dedupes the repeats
    — the ATS, not us, decides how many actually land; the UI warns about this. It is also routed
    through the per-company velocity cap (like the other kinds) so it can't fire `per_day` (≤100)
    applications at one company/run — the exact distinct-identity cluster the cap exists to prevent.
  * JOBS campaign (a checkbox selection on /catalog): `job_ids` is the owner's ordered pick, walked
    ROUND-ROBIN by a persisted `cursor` — up to `remaining_today` ids per run starting at the cursor,
    the same UN-LANDED job repeating only after every eligible one was hit (per_day > eligible count
    may repeat within a day; the ATS dedupes on the one mailbox). The cursor advances by the last job
    ATTEMPTED, so a failed fill is retried next run instead of skipped. Ids whose catalog row is
    missing or `dead` are skipped for THAT run (not removed — a posting can come back), and the
    applied/submitted exclusions of the search kind do NOT apply (the owner chose these jobs; a cycle
    revisits the UN-LANDED ones). Three guards keep the daily budget honest: (1) a job the campaign
    already LANDED (confirmed submit, in `confirmed_jobids`) is NEVER re-served — no weekly re-spam of
    an accepted posting; (2) a per-day ATTEMPT ceiling (per_day × CAMPAIGN_MAX_ATTEMPTS_FACTOR) so a
    0-yield day can't sweep unlimited full fills; (3) by default only the auto-submittable ATSes
    (greenhouse/ashby) are attempted — the captcha-walled ones (lever/workable) are skipped unless a
    solver + clean egress is live (CAMPAIGN_SOLVE_CAPTCHA=1). The name may be left EMPTY — one is then
    generated for the first selected job's country (see `_generated_name`) and pinned like a typed one.
  * ONE stable mailbox per campaign (`email`/`pid` pinned at create) so all of a campaign's replies
    land in a single inbox and the applications read as one coherent person.
Pure/offline helpers are unit-tested in backend/tests/test_apply_campaigns.py (no DB/network).
"""
from __future__ import annotations

import fcntl
import json
import os
import random
import re
import threading
from contextlib import contextmanager
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "apply_campaigns.json"
_LOCK = threading.RLock()
# Per-application JOURNAL: one row per apply attempt with its outcome, so the dashboard can show a
# campaign's successes/fails (owner: «где посмотреть журнал успехов/фейлов»). Written by the cron.
_EVENTS_PATH = Path(__file__).resolve().parent.parent / "data" / "apply_campaign_events.json"
_EVENTS_CAP = 1000
_OUTCOMES = ("confirmed", "spam", "needs_correction", "dead", "clicked", "error", "skipped")


@contextmanager
def _file_lock():
    """Cross-PROCESS guard for load-mutate-save: the cron (`apply_campaign_cron`, its own process)
    and the dashboard both rewrite the whole file, and the RLock only covers one process. An
    fcntl lock on a sidecar file serialises them; the write itself stays atomic (tmp + replace)."""
    _PATH.parent.mkdir(exist_ok=True)
    lock_path = _PATH.with_suffix(".lock")
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX)
        except OSError:
            pass          # a filesystem without flock — fall back to the in-process lock only
        try:
            yield
        finally:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            except OSError:
                pass
# Only these ATSes auto-submit end-to-end from the datacenter IP (emailed code, not a live captcha),
# so a SEARCH campaign only targets them — else the budget is spent on jobs that can't complete.
_AUTO_ATS = {"greenhouse", "ashby"}

# A `jobs` campaign caps the DAILY ATTEMPTS (every fill tried, not just the landings) at
# per_day × this factor, so a 0-yield day can't sweep unlimited full fills — per_day alone is a
# CONFIRMED-submit cap (note_run bumps runs_today only by the landings). Env-overridable.
CAMPAIGN_MAX_ATTEMPTS_FACTOR = int(os.environ.get("CAMPAIGN_MAX_ATTEMPTS_FACTOR") or 4)


def _solve_captcha_on() -> bool:
    """Whether a captcha solver + a clean (residential/phone) egress is available — the 'attempt
    everything once phones + NopeCHA are live' switch (env CAMPAIGN_SOLVE_CAPTCHA=1). When OFF
    (the default) a `jobs` campaign skips the captcha-walled ATSes (lever/workable) so its budget
    concentrates on the submittable greenhouse/ashby jobs."""
    return (os.environ.get("CAMPAIGN_SOLVE_CAPTCHA") or "").strip().lower() in ("1", "true", "yes", "on")


def _max_attempts_today(camp: dict) -> int:
    """The per-day ATTEMPT ceiling for a campaign = per_day × CAMPAIGN_MAX_ATTEMPTS_FACTOR (the
    factor is read at call time so tests may monkeypatch it)."""
    return max(1, int(camp.get("per_day", 1) or 1)) * int(CAMPAIGN_MAX_ATTEMPTS_FACTOR)


def _load() -> list:
    try:
        d = json.loads(_PATH.read_text())
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(rows: list) -> None:
    _PATH.parent.mkdir(exist_ok=True)
    tmp = _PATH.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(rows, ensure_ascii=False))
    os.replace(tmp, _PATH)


def _next_id(rows: list) -> int:
    return (max((int(r.get("id", 0)) for r in rows), default=0) + 1)


def _stable_email(name: str, cid: int) -> str:
    """One durable @takhet.com mailbox for the whole campaign (so replies aggregate)."""
    from backend.tools.catalog_drafts import derive_email
    base = derive_email(name) or ""
    if not base:
        return f"campaign.c{cid}@takhet.com"
    local, _, dom = base.partition("@")
    return f"{local}.c{cid}@{dom or 'takhet.com'}"


def next_identity(cid: int, exists=None) -> tuple[str, str]:
    """Per-APPLICATION identity for a campaign (owner 2026-09-09: «пусть почта меняется на каждую
    подачу»): the NAME stays the campaign's, every application gets a FRESH @takhet.com mailbox +
    persona id — `first.last<N>@` like any synthetic persona, N from a per-campaign base counter,
    skipping addresses the CRM already knows. `seq` is persisted BEFORE the fill so a crash never
    reuses a mailbox. A campaign with `email_mode != 'per_apply'` keeps its one fixed mailbox."""
    if exists is None:
        def exists(email: str) -> bool:
            try:
                from backend.tools import mailcrm
                return any((c.get("email") or "").lower() == email.lower() for c in mailcrm.candidates())
            except Exception:
                return False
    with _LOCK, _file_lock():
        rows = _load()
        camp = next((r for r in rows if int(r.get("id", 0)) == int(cid)), None)
        if camp is None:
            raise KeyError(cid)
        if camp.get("email_mode", "per_apply") != "per_apply":
            return camp["email"], camp["pid"]
        from backend.tools.catalog_drafts import derive_email
        base = derive_email(camp.get("name") or "") or f"campaign.c{cid}@takhet.com"
        local, _, dom = base.partition("@")
        dom = dom or "takhet.com"
        if not camp.get("seq_base"):
            camp["seq_base"] = random.randint(120, 9000)
        n = int(camp["seq_base"])
        for _ in range(50):
            camp["seq"] = int(camp.get("seq") or 0) + 1
            n = int(camp["seq_base"]) + int(camp["seq"])
            email = f"{local}{n}@{dom}"
            if not exists(email):
                break
        slug = re.sub(r"[^a-z0-9]+", "", (camp.get("name") or "").lower())[:16] or "x"
        pid = f"demo_camp{cid}_{slug}_{n}"
        _save(rows)
        return email, pid


def list_campaigns() -> list:
    with _LOCK:
        return _load()


def _parse_job_ids(job_ids) -> list[int]:
    """The owner's selection as a de-duplicated, order-preserving list of ints. Accepts a list/
    tuple/set of ints (or int-strings) OR the form-encoded "1,2,3" string the /catalog sheet POSTs.
    Blank tokens are ignored; a non-numeric token is a ValueError (a typo must not silently vanish
    from the selection)."""
    if isinstance(job_ids, str):
        toks = [t for t in re.split(r"[,\s]+", job_ids.strip()) if t]
    else:
        toks = [t for t in (job_ids or []) if t is not None and str(t).strip()]
    try:
        return list(dict.fromkeys(int(str(t).strip()) for t in toks))
    except (TypeError, ValueError):
        raise ValueError("job_ids must be integers") from None


def interleave_by_company(job_ids, jobs_by_ids=None) -> list[int]:
    """Re-order a `jobs` selection so the daily budget spans DIFFERENT companies instead of
    marching through one company's jobs for days (the /catalog list is sorted company-ASC, so a
    243-job pick starting with 14 Salmon then 15 Supabase … would take weeks per company at a low
    per_day). Groups by company (first-seen order, within-group order preserved) then round-robins
    across the groups: binance, nogigiddy, Supabase, Salmon, binance, … `jobs_by_ids` is injectable
    for tests; a DB miss degrades to the original order (all ids fall in one '?' bucket)."""
    ids = [int(x) for x in (job_ids or [])]
    if not ids:
        return []
    if jobs_by_ids is None:
        from backend.tools.catalog_db import jobs_by_ids as jobs_by_ids
    try:
        rows = jobs_by_ids(ids) or {}
    except Exception:
        rows = {}
    groups: dict[str, list[int]] = {}
    order: list[str] = []
    for j in ids:
        co = (((rows.get(j) or {}).get("company") or "?") or "?").strip() or "?"
        if co not in groups:
            groups[co] = []
            order.append(co)
        groups[co].append(j)
    out: list[int] = []
    while any(groups[co] for co in order):
        for co in order:
            if groups[co]:
                out.append(groups[co].pop(0))
    return out


# AUTO spelling-variants for ANY campaign name (no per-name setup): a fixed exact name string on
# every application is what saturated Ashby's per-tenant identity cluster; rotating the SPELLING
# (Latin/Cyrillic transliteration + Y-glide forms) with a unique LinkedIn per fill + the 2/day
# company cap keeps each tenant under the threshold while the applicant stays recognizably the same
# person. `_NAME_VARIANTS` is an OPTIONAL hand-tuned override (empty by default — the generator
# below handles every name automatically so a NEW campaign needs nothing extra).
_NAME_VARIANTS: dict[str, list[str]] = {}

_LAT2CYR_DI = [("shch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"),
               ("th", "т"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"), ("ye", "е"), ("ph", "ф")]
_LAT2CYR = {"a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х",
            "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
            "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
            "y": "й", "z": "з"}
_CYR2LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
            "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
            "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
            "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
            "я": "ya"}


def _translit_word(w: str, to_cyr: bool) -> str:
    lo = w.lower()
    res = ""
    if to_cyr:
        i = 0
        while i < len(lo):
            for di, cy in _LAT2CYR_DI:
                if lo.startswith(di, i):
                    res += cy
                    i += len(di)
                    break
            else:
                res += _LAT2CYR.get(lo[i], lo[i])
                i += 1
    else:
        res = "".join(_CYR2LAT.get(ch, ch) for ch in lo)
    return res[:1].upper() + res[1:] if res else res


def _translit(s: str, to_cyr: bool) -> str:
    return " ".join(_translit_word(w, to_cyr) for w in s.split())


def _y_forms(base: str) -> set[str]:
    """Toggle a leading E↔Ye on each name part (Erlan↔Yerlan) — the 'sometimes add a Y' the owner
    asked for, applied generally to any name."""
    out: set[str] = set()
    parts = base.split()
    for i, p in enumerate(parts):
        alt = None
        if p[:2].lower() == "ye" and len(p) > 2:
            alt = p[1:2].upper() + p[2:]                          # Yerlan -> Erlan (keep caps)
        elif p[:1].lower() == "e" and len(p) > 1:
            alt = ("Y" if p[:1].isupper() else "y") + p[:1].lower() + p[1:]  # Erlan -> Yerlan
        if alt:
            out.add(" ".join(parts[:i] + [alt] + parts[i + 1:]))
    return out


def spelling_variants(base: str) -> list[str]:
    """All spelling variants of a name, auto-generated for ANY input (Latin↔Cyrillic + Y-forms)."""
    base = (base or "").strip()
    if not base:
        return []
    curated = _NAME_VARIANTS.get(base.lower())
    if curated:
        return list(curated)
    is_cyr = any("Ѐ" <= ch <= "ӿ" for ch in base)
    variants = {base}
    latin_forms = {base} if not is_cyr else {_translit(base, to_cyr=False)}
    for lf in list(latin_forms):
        latin_forms |= _y_forms(lf)
    variants |= latin_forms
    for lf in latin_forms:                               # a Cyrillic spelling of each Latin form
        variants.add(_translit(lf, to_cyr=True))
    return sorted(v for v in variants if v)


def name_variant(base: str) -> str:
    """Pick a random spelling variant of a campaign name — works for ANY name with no setup."""
    import random as _random
    vs = spelling_variants(base)
    return _random.choice(vs) if vs else (base or "")


def _generated_name(job_ids: list[int], gender: str) -> str:
    """A fresh persona name for a `jobs` campaign the owner left unnamed: random from
    synth_persona's per-country banks, the country taken from the FIRST selected job's catalog row
    (`_country_of`, so the name fits the posting like a one-click fill would), 'United States' when
    the lookup fails or finds nothing. Random at create, then pinned on the row forever (the
    campaign's identity), like a typed name. Both lookups are lazy imports so the store itself
    never touches the DB / persona machinery; tests monkeypatch this function."""
    country = "United States"
    try:
        from backend.tools.catalog_db import jobs_by_ids
        from backend.tools.synth_persona import _country_of
        row = (jobs_by_ids(job_ids[:1]) or {}).get(int(job_ids[0])) if job_ids else None
        if row:
            country = _country_of(row) or country
    except Exception:
        pass
    from backend.tools.synth_persona import _pick_name
    return _pick_name(country, gender or "male")


def create(*, name: str, target_kind: str, job_id=None, job_ids=None, q: str = "",
           region: str = "", gender: str = "", per_day: int = 1, today: str = "",
           name_for=None) -> dict:
    """Create a campaign. `target_kind` ∈ {'job','search','jobs'}. `job` needs `job_id`; `jobs`
    needs a non-empty `job_ids` selection (list of ints or "1,2,3") and may leave `name` empty —
    it is then generated by `name_for(job_ids, gender)` (default: module `_generated_name`). The
    other kinds require a name. per_day is clamped 1..5. `today` (YYYY-MM-DD) seeds
    last_run_date; pass the current date (the store never calls Date.now itself)."""
    name = (name or "").strip()
    if target_kind not in ("job", "search", "jobs"):
        raise ValueError("target_kind must be 'job', 'search' or 'jobs'")
    gender = gender if gender in ("male", "female") else ""
    try:
        per_day = max(1, min(int(per_day), 100))    # owner: a free number, not a 1-5 picker
    except (TypeError, ValueError):
        per_day = 1
    if target_kind == "job" and not job_id:
        raise ValueError("job_id required for a single-job campaign")
    ids: list[int] = []
    if target_kind == "jobs":
        ids = _parse_job_ids(job_ids)
        if not ids:
            raise ValueError("job_ids required for a jobs campaign")
        if not name:
            # Look the module attribute up at call time so a monkeypatched `_generated_name`
            # is honoured even when the caller didn't pass `name_for`.
            name = ((name_for or _generated_name)(ids, gender) or "").strip()
    if not name:
        raise ValueError("name required")
    with _LOCK, _file_lock():
        rows = _load()
        cid = _next_id(rows)
        camp = {
            "id": cid, "name": name, "target_kind": target_kind,
            "job_id": int(job_id) if job_id else None,
            "q": (q or "").strip(), "region": (region or "").strip(),
            "gender": gender,
            "per_day": per_day,
            "email": _stable_email(name, cid),
            "pid": f"demo_camp{cid}_{re.sub(r'[^a-z0-9]+', '', name.lower())[:16] or 'x'}",
            # owner 2026-09-09: a NEW mailbox per application (same name) — see next_identity().
            # EXCEPTION: a single-`job` campaign keeps ONE stable mailbox ("fixed"). It applies
            # `per_day` times to ONE posting, and a distinct identity per fill would (a) defeat the
            # ATS dedup that keeps those repeats harmless and (b) — combined with the velocity-cap
            # bypass the job branch used to have — reproduce the exact 149-distinct-identity cluster
            # on one company the per-company cap exists to make impossible. The `[jid]*n` semantics
            # stay; only the identity is pinned. search/jobs stay per_apply (they route through the
            # velocity cap, so a fresh identity per fill is safe + intentional).
            "email_mode": "fixed" if target_kind == "job" else "per_apply",
            "seq": 0, "seq_base": random.randint(120, 9000),
            "active": True, "applied_jobids": [], "runs_today": 0, "attempts_today": 0,
            "last_run_date": today or "", "created": today or "",
        }
        if target_kind == "jobs":
            # spread the selection across companies from day one (a DB miss keeps the given order)
            camp["job_ids"] = interleave_by_company(ids)
            camp["cursor"] = 0
            camp["confirmed_jobids"] = []      # jobs this campaign has LANDED — never re-served
        rows.append(camp)
        _save(rows)
        return camp


def delete(cid: int) -> bool:
    with _LOCK, _file_lock():
        rows = _load()
        new = [r for r in rows if int(r.get("id", 0)) != int(cid)]
        if len(new) == len(rows):
            return False
        _save(new)
        return True


def set_active(cid: int, active: bool) -> bool:
    with _LOCK, _file_lock():
        rows = _load()
        hit = False
        for r in rows:
            if int(r.get("id", 0)) == int(cid):
                r["active"] = bool(active)
                hit = True
        if hit:
            _save(rows)
        return hit


def quarantine_jobs(cid: int, jobids, on: bool = True) -> int:
    """Hold aside (on=True) or release (on=False) job ids in a 'jobs' campaign's quarantine set,
    so resolve_targets never serves them (see _eligible). Use for postings un-landable from the
    current egress — a hard captcha wall (binance-Lever) or a velocity/spam-quarantining tenant —
    so the daily budget isn't burned re-attempting them and we don't worsen the ATS's velocity
    flag by re-spamming. Fully reversible (call with on=False once a clean mobile/residential IP is
    live). Returns the number of ids actually added/removed. Locked read-mutate-save like note_run."""
    want = {int(x) for x in (jobids or [])}
    if not want:
        return 0
    changed = 0
    with _LOCK, _file_lock():
        rows = _load()
        for r in rows:
            if int(r.get("id", 0)) != int(cid):
                continue
            have = {int(x) for x in (r.get("quarantine_jobids") or [])}
            new = (have | want) if on else (have - want)
            if new != have:
                changed = len(new ^ have)
                r["quarantine_jobids"] = sorted(new)
        if changed:
            _save(rows)
    return changed


def _roll_day(camp: dict, today: str) -> None:
    """Reset the per-day counters when the date changed (mutates camp in place; caller persists)."""
    if camp.get("last_run_date") != today:
        camp["runs_today"] = 0
        camp["attempts_today"] = 0
        camp["last_run_date"] = today


def remaining_today(camp: dict, today: str) -> int:
    """How many more applications this campaign may make today (0 if inactive). TWO ceilings, both
    reset daily: the CONFIRMED cap `per_day` (runs_today counts the landings) AND a per-day ATTEMPT
    cap `per_day × CAMPAIGN_MAX_ATTEMPTS_FACTOR` (attempts_today counts every fill tried) — so an
    infeasible, 0-yield day can't sweep unlimited full fills. The smaller headroom wins."""
    if not camp.get("active"):
        return 0
    same_day = camp.get("last_run_date") == today
    runs = int(camp.get("runs_today", 0)) if same_day else 0
    attempts = int(camp.get("attempts_today", 0)) if same_day else 0
    by_confirmed = int(camp.get("per_day", 1)) - runs
    by_attempts = _max_attempts_today(camp) - attempts
    return max(0, min(by_confirmed, by_attempts))


def _jobs_rows(ids: list[int], jobs_by_ids) -> tuple[dict, bool]:
    """Fetch {id: row} for the selection ONCE. Returns (rows, have_rows); a lookup failure (DB down)
    yields ({}, False) so callers treat every id as alive/eligible rather than starving the campaign
    — the fill itself then reports a dead/uncompletable page. `jobs_by_ids` is injectable for tests."""
    if jobs_by_ids is None:
        from backend.tools.catalog_db import jobs_by_ids
    try:
        return (jobs_by_ids(ids) or {}), True
    except Exception:
        return {}, False


def _alive_ids(ids: list[int], jobs_by_ids, *, rows=None, have_rows=None) -> list[int]:
    """The selection minus ids whose catalog row is missing or `dead` (a posting gone at the ATS —
    `catalog_db.mark_dead`). Order preserved. A lookup failure (DB down) treats every id as alive
    rather than starving the campaign — the fill itself then reports a dead page. `rows`/`have_rows`
    may be pre-fetched by the caller (via `_jobs_rows`) to avoid a second DB round-trip."""
    if rows is None:
        rows, have_rows = _jobs_rows(ids, jobs_by_ids)
    if not have_rows:
        return list(ids)
    return [j for j in ids if j in rows and not (rows[j] or {}).get("dead")]


def _landed_checker(ids, landed):
    """A `furthest_stage_of(jobid) -> stage|None` for the STOP-ON-RESPONSE guard. An injected
    `landed` callable wins (tests); else the guarded live `offer_priority.landed_check` over
    `ids` (returns None for everything when uploads/prefill or the mail DB is unavailable — e.g.
    a worktree — so nothing is ever WRONGLY treated as landed). Never raises."""
    if landed is not None:
        return landed
    try:
        from backend.tools.offer_priority import landed_check
        return landed_check([int(x) for x in ids])
    except Exception:
        return lambda j: None


def resolve_targets(camp: dict, today: str, *, list_jobs=None, submitted=None,
                    jobs_by_ids=None, velocity_guard=None, landed=None, order=None) -> list[int]:
    """The job ids to apply to on THIS run, honoring the per-day budget. Single-job → [job_id]
    × remaining. Jobs (a /catalog selection) → the next `remaining_today` ids round-robin from
    the persisted `cursor`, skipping (for this run only) rows that are missing/dead, already-LANDED
    (`confirmed_jobids`), or — unless CAMPAIGN_SOLVE_CAPTCHA=1 — on a captcha-walled ATS. Search → up
    to `remaining_today` NEW jobs from list_jobs(q, region) that this campaign hasn't already applied
    to and that aren't globally submitted. `list_jobs`/`submitted`/`jobs_by_ids` are injectable
    for testing; default to the live catalog_db/bulk_log. `velocity_guard` (injectable; default
    `company_velocity.guard`) is the PER-COMPANY cap shared with the bulk drain: it drops companies
    already over `COMPANY_CAP_PER_DAY`/`_PER_WEEK` and limits this run to each company's remaining
    budget — the guard that makes a 149-fills-on-Salmon re-hammer impossible from any path.

    STOP-ON-RESPONSE (owner 2026-09-20): a position whose furthest inbound mail stage is already
    interview/offer is dropped from every kind — once a posting produced the outcome we want, we
    stop spending fresh personas on it. `landed` (a `jobid->stage|None` callable) is injectable;
    the default is the guarded live `offer_priority.landed_check` (nothing landed when uploads/mail
    are unavailable). `order` (default env `APPLY_ORDER`=pay_desc) pay-orders the SEARCH pool so the
    highest-paying eligible NEW jobs are applied first (the `jobs` kind keeps the owner's cursor
    round-robin so pay-ordering there would fight the rotation)."""
    n = remaining_today(camp, today)
    if n <= 0:
        return []
    if velocity_guard is None:
        from backend.tools.company_velocity import guard as velocity_guard
    try:
        from backend.tools import offer_priority as _op
    except Exception:
        _op = None

    def _is_landed(stage_of, j) -> bool:
        """True iff position j already reached interview/offer (guarded; never raises)."""
        if _op is None:
            return False
        try:
            return _op.is_landed(stage_of(int(j)))
        except Exception:
            return False

    def _capped(cands: list[int]) -> list[int]:
        try:
            kept, _ = velocity_guard(cands, jobs_by_ids=jobs_by_ids)
        except TypeError:
            kept = velocity_guard(cands)
            kept = kept[0] if isinstance(kept, tuple) else kept
        return [int(x) for x in kept][:n]
    applied = set(int(x) for x in (camp.get("applied_jobids") or []))
    kind = camp.get("target_kind")
    if kind == "job":
        # single job: apply per_day times TODAY (owner-requested; same name, fresh résumé each)
        # under ONE stable email (email_mode='fixed', pinned at create), so the ATS dedupes the
        # repeats — the employer/ATS, not us, decides how many of the N/day actually land. Routed
        # through the SAME per-company velocity cap as the other kinds so a job campaign can't fire
        # `per_day` (≤100) uncapped applications at one company/run: `_capped` drops it to that
        # company's remaining daily budget (and to [] once the company is over cap).
        jid = camp.get("job_id")
        if not jid:
            return []
        # STOP-ON-RESPONSE: if this one posting already reached interview/offer, apply no more.
        if _is_landed(_landed_checker([int(jid)], landed), int(jid)):
            return []
        return _capped([int(jid)] * n)
    if kind == "jobs":
        # owner-picked set: round-robin from the cursor. The applied/`submitted` exclusions of the
        # search kind do NOT apply (the owner chose these jobs), BUT a job this campaign already
        # LANDED (a confirmed submit, in `confirmed_jobids`) is NEVER re-served — else an accepted
        # posting gets re-spammed every week. The cursor is a position in the FULL selection: walk it
        # from there, SKIPPING ids that are missing/dead for this run (never dropped — a posting can
        # come back), already-confirmed, OR (by default) on a captcha-walled ATS the datacenter IP
        # can't auto-submit (lever/workable) — unless CAMPAIGN_SOLVE_CAPTCHA=1 says a solver + clean
        # egress is live. Wrap until `n` targets are collected. note_run then places the cursor right
        # after the last job ATTEMPTED, so both sides index the same list.
        ids = [int(x) for x in (camp.get("job_ids") or [])]
        if not ids:
            return []
        rows, have_rows = _jobs_rows(ids, jobs_by_ids)
        alive = set(_alive_ids(ids, jobs_by_ids, rows=rows, have_rows=have_rows))
        confirmed = set(int(x) for x in (camp.get("confirmed_jobids") or []))
        # Quarantined jobs are held aside as un-landable from the current egress (e.g. a hard
        # invisible-captcha wall like binance-Lever, or a company that velocity/spam-quarantines
        # our datacenter IP). Skipped UNCONDITIONALLY — unlike the captcha-ATS skip below, a
        # quarantined id is never served even when CAMPAIGN_SOLVE_CAPTCHA=1, so enabling the solver
        # for one ATS can't re-hammer a wall we've deliberately parked. Reversible via quarantine_jobs.
        quarantined = set(int(x) for x in (camp.get("quarantine_jobids") or []))
        solve = _solve_captcha_on()
        _stage_of = _landed_checker(ids, landed)   # STOP-ON-RESPONSE: interview/offer → skip

        def _eligible(j: int) -> bool:
            if j not in alive or j in confirmed or j in quarantined:
                return False
            if _is_landed(_stage_of, j):        # already produced an interview/offer — stop
                return False
            # captcha-walled ATS skip: only when we actually know the ATS (have_rows) and no solver
            if not solve and have_rows and (rows.get(j) or {}).get("ats") not in _AUTO_ATS:
                return False
            return True

        if not any(_eligible(j) for j in ids):
            return []
        # Oversample (3×n) so an over-cap company in the selection can't starve the run: the
        # velocity guard trims per company, then we keep the first n. The cursor is placed by
        # note_run after the last job ATTEMPTED, so trimmed ids are simply re-walked next run.
        out, pos = [], int(camp.get("cursor") or 0) % len(ids)
        want = n * 3
        for _ in range(want * len(ids) + len(ids)):   # bounded: at most `want` full laps
            j = ids[pos % len(ids)]
            pos += 1
            if _eligible(j):
                out.append(j)
                if len(out) >= want:
                    break
        return _capped(out)
    # search campaign
    if list_jobs is None:
        from backend.tools.catalog_db import list_jobs as list_jobs
    if submitted is None:
        from backend.tools.bulk_log import submitted_jobids
        submitted = submitted_jobids()
    submitted = set(int(x) for x in (submitted or []))
    # Fetch a WIDE window, not n*8: list_jobs can't filter by ATS, and in a captcha-heavy eligibility
    # slice (e.g. q='Kazakhstan' is ~80% Lever/Workable) the auto-ATS greenhouse/ashby rows we can
    # actually submit are SPARSE and sorted PAST a small window — a 120-row limit filled up with
    # captcha jobs we discard, surfacing 0 fresh auto-ATS and starving the campaign to "nothing to
    # apply" while real fresh jobs sat at position >120. The in-Python auto-ATS filter + applied/
    # submitted exclusions + the n*3 oversample + the per-company velocity cap still bound the output;
    # this only widens what the resolver can SEE. (Bounded so a huge q='' pool stays cheap.)
    rows = list_jobs(q=(camp.get("q") or None), region=(camp.get("region") or None),
                     remote_only=True, limit=max(n * 8, 2000))
    # HIGH-PAY FIRST (owner 2026-09-20): order the eligible pool by disclosed pay DESC (env
    # APPLY_ORDER, default pay_desc) so the best-paying fresh jobs are attempted first. Stable
    # (a no-comp pool keeps list_jobs order). Guarded — a bad row/module leaves the order as-is.
    if _op is not None:
        try:
            rows = _op.order_by_pay(list(rows), _op.pay_key_catalog, order=order)
        except Exception:
            pass
    # STOP-ON-RESPONSE checker over the candidate ids in this window.
    _stage_of = _landed_checker([r.get("id") or r.get("jobid") or 0 for r in rows], landed)
    # per_job_per_day (owner opt-in, default 1): apply to EACH eligible vacancy this many times per
    # day under the campaign's fixed name. >1 is the owner's explicit "3×/vacancy, same name" mode —
    # it RE-APPLIES already-applied jobs and BYPASSES the per-company velocity cap (that cap exists to
    # prevent exactly this, so the owner override turns it off for this campaign only). Captcha ATS
    # (Lever/Workable) stay excluded regardless. NB the ATS/recruiter dedupes repeat applications by
    # identity, so extra same-name submits to one job rarely add a real chance and raise the spam-flag
    # risk — this is a deliberate owner setting, not the default.
    per_job = max(1, int(camp.get("per_job_per_day") or 1))
    out = []
    for r in rows:
        # Only greenhouse/ashby auto-submit end-to-end from the datacenter IP (email-code, not a
        # live captcha); Lever/Workable would be filled but never submitted. Always excluded.
        if (r.get("ats") or "") not in _AUTO_ATS:
            continue
        jid = int(r.get("id") or r.get("jobid") or 0)
        if not jid:
            continue
        if _is_landed(_stage_of, jid):   # already reached interview/offer — stop applying to it
            continue
        if per_job <= 1 and (jid in applied or jid in submitted):
            continue                     # normal mode: never re-apply a job
        out.extend([jid] * per_job)      # per_job>1: emit each vacancy that many times (re-apply ok)
        if len(out) >= n * 3:            # oversample; trimmed below
            break
    if per_job > 1:
        return out[:n]                   # owner override: velocity cap bypassed, capped only by per_day
    return _capped(out)


def note_run(cid: int, jobids, today: str, attempted=None) -> None:
    """Record an executed run: `jobids` = the jobs that really LANDED (a CONFIRMED submit per
    `fill_counts_as_done` — they spend the day's per_day budget and join applied_jobids); `attempted`
    = every job the run tried (default: the same list). All attempted jobs count toward the per-day
    ATTEMPT ceiling (`attempts_today`). A `jobs` campaign additionally: (a) records the landings in
    `confirmed_jobids` so resolve_targets never re-serves an accepted posting, and (b) moves its
    rotation `cursor` past the LAST ATTEMPTED job, so a job that failed today (dead, incomplete,
    error) doesn't pin the rotation — it comes round again next lap, and a dead one is skipped by
    then (the cron marks it dead)."""
    jobids = [int(j) for j in (jobids or [])]
    attempted = [int(j) for j in (attempted if attempted is not None else jobids)]
    with _LOCK, _file_lock():
        rows = _load()
        for r in rows:
            if int(r.get("id", 0)) == int(cid):
                _roll_day(r, today)
                have = set(int(x) for x in (r.get("applied_jobids") or []))
                have.update(jobids)
                r["applied_jobids"] = sorted(have)
                r["runs_today"] = int(r.get("runs_today", 0)) + len(jobids)
                # every fill TRIED (landed or not) counts toward the per-day ATTEMPT ceiling
                r["attempts_today"] = int(r.get("attempts_today", 0)) + len(attempted)
                r["last_run_date"] = today
                if r.get("target_kind") == "jobs":
                    # a jobs campaign never re-applies a job it LANDED (`jobids` is confirmed-only,
                    # per fill_counts_as_done) — resolve_targets skips these from now on.
                    conf = set(int(x) for x in (r.get("confirmed_jobids") or []))
                    conf.update(jobids)
                    r["confirmed_jobids"] = sorted(conf)
                    if attempted:
                        # the rotation resumes right AFTER the last job attempted — the same position
                        # space resolve_targets walks (dead ids were skipped, not counted)
                        sel = [int(x) for x in (r.get("job_ids") or [])]
                        if sel and attempted[-1] in sel:
                            r["cursor"] = (sel.index(attempted[-1]) + 1) % len(sel)
        _save(rows)


def fill_counts_as_done(fill_state: dict | None) -> bool:
    """Whether a `dashboard_app._do_fill` outcome (its `_FILL_JOBS[jid]` entry) counts as an
    APPLICATION — i.e. spends the day's budget. Only a fill that really pressed Submit (the
    co-pilot's `submit_result.clicked`/`confirmed`) counts; a filled-but-not-submitted form
    (`incomplete`/`needs_review`), a dead posting (`no_form`) or an `error` does not (the first
    live campaign run billed a `no_form` on a posting gone from the ATS as one of its 3/day)."""
    st = fill_state or {}
    if st.get("state") != "done":
        return False
    sub = st.get("submit")
    if sub is None:
        return True          # a fill without a submit phase (dry-run style) — nothing to judge
    # A pressed Submit is NOT an application on GH/Ashby: the ATS still needs the emailed code
    # (the cron waits for it inline) and may reject the submit outright (Ashby's datacenter-IP
    # «flagged as possible spam» — run #2 of the first campaign: 3 clicked, 0 accepted). Only a
    # CONFIRMED submit counts; a `blocked` one never does.
    if sub.get("blocked"):
        return False
    return bool(sub.get("confirmed"))


def fill_is_dead_posting(fill_state: dict | None) -> bool:
    """The co-pilot POSITIVELY classified the page as terminal (`submit_result.reason == 'no_form'`
    AND `page_type in {expired, login_required, captcha}`): a 404 / 'job not found' / 'no longer
    available' / login-wall / captcha-wall page — mark it dead so the rotation skips it.

    A bare zero-field load whose `page_type` is None / 'unknown' / 'application_form' / 'job_listing'
    is NOT proof the posting is gone: it is what a SLOW or flapping egress render looks like (the
    form's async fetch didn't finish inside the co-pilot's field-poll window). Marking those dead
    permanently kills LIVE catalog rows — a slow phone-egress SOCKS load once killed Render 20282,
    a job still live on Render's Ashby board that same day. Those are retried on a later lap, never
    permanently killed. `analyze_page` early-returns filled=0 ONLY for the three positive terminal
    types, so restricting to them is the exact 'the page said it's gone/walled' signal."""
    sub = ((fill_state or {}).get("submit") or {})
    return (sub.get("reason") == "no_form"
            and sub.get("page_type") in ("expired", "login_required", "captcha"))


# ---- per-application JOURNAL (successes / fails, shown in the dashboard) --------------------------
def outcome_from_fill_state(fill_state: dict | None) -> tuple[str, str]:
    """Map a `_FILL_JOBS[jid]` outcome to a (journal outcome, detail) pair. PURE (no DB). Outcome ∈
    `_OUTCOMES`: confirmed (a real accepted submit) · dead (posting gone, no form) · spam (ATS
    'flagged as spam / couldn't submit') · needs_correction (missing-required rejection) · clicked
    (Submit pressed, no confirmation) · error (the fill crashed or refused to submit)."""
    st = fill_state or {}
    if fill_counts_as_done(st):
        return ("confirmed", "")
    if fill_is_dead_posting(st):
        return ("dead", "")
    sub = st.get("submit") or {}
    blocked = str(sub.get("blocked") or "")
    if blocked:
        b = blocked.lower()
        if "spam" in b or "couldn't submit" in b or "could not submit" in b or "couldnt submit" in b:
            return ("spam", blocked[:200])
        if "correction" in b or "missing entry" in b:
            return ("needs_correction", blocked[:200])
        return ("clicked", blocked[:200])
    if st.get("state") == "error":
        return ("error", str(st.get("error") or "")[:200])
    if sub.get("clicked"):
        return ("clicked", "")
    return ("error", "не отправлено")


def _events_load() -> list:
    try:
        d = json.loads(_EVENTS_PATH.read_text())
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _events_save(rows: list) -> None:
    _EVENTS_PATH.parent.mkdir(exist_ok=True)
    tmp = _EVENTS_PATH.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(rows[-_EVENTS_CAP:], ensure_ascii=False))
    os.replace(tmp, _EVENTS_PATH)


def log_event(cid, job_id, *, company: str = "", title: str = "", mailbox: str = "",
              outcome: str = "clicked", detail: str = "", today: str = "", ts: str = "") -> dict:
    """Append one journal row (kept to the most-recent `_EVENTS_CAP`). ts/today are passed in so the
    store never calls the clock (unit-testable). Shares the cross-process `_file_lock()`."""
    ev = {"ts": ts or today or "", "cid": int(cid), "job_id": int(job_id),
          "company": company or "", "title": title or "", "mailbox": mailbox or "",
          "outcome": outcome if outcome in _OUTCOMES else "clicked", "detail": detail or ""}
    with _LOCK, _file_lock():
        rows = _events_load()
        rows.append(ev)
        _events_save(rows)
    return ev


def list_events(cid=None, limit: int = 200) -> list:
    """Journal rows NEWEST-first (optionally for one campaign)."""
    with _LOCK:
        rows = _events_load()
    if cid is not None:
        rows = [e for e in rows if int(e.get("cid", 0)) == int(cid)]
    rows = list(reversed(rows))
    return rows[: max(0, int(limit))] if limit else rows


def event_summary(cid=None) -> dict:
    """Per-outcome tally (for one campaign or all) + `total`."""
    with _LOCK:
        rows = _events_load()
    if cid is not None:
        rows = [e for e in rows if int(e.get("cid", 0)) == int(cid)]
    out = {k: 0 for k in _OUTCOMES}
    for e in rows:
        o = e.get("outcome")
        if o in out:
            out[o] += 1
    out["total"] = len(rows)
    return out


_LOG_AS_RE = re.compile(r"campaign (\d+) job (\d+) as .*?<([^>]+)>")
_LOG_RES_RE = re.compile(r"campaign (\d+) job (\d+) -> (.*)$")
_LOG_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def _outcome_from_log(rest: str) -> tuple[str, str]:
    r = rest.lower()
    if "confirmed" in r:
        return ("confirmed", "")
    if "no_form" in r:
        return ("dead", "")
    if "blocked=" in rest:
        detail = rest.split("blocked=", 1)[1].strip()
        b = detail.lower()
        if "spam" in b or "couldn't submit" in b or "could not submit" in b:
            return ("spam", detail[:200])
        if "correction" in b or "missing entry" in b:
            return ("needs_correction", detail[:200])
        return ("clicked", detail[:200])
    if "error" in r and "submit=" not in r:
        return ("error", "")
    return ("clicked", "")


def backfill_events_from_log(path: str = "logs/apply_campaigns.log", *, text: str | None = None,
                             jobs_by_ids=None) -> int:
    """Parse the cron's text log into the structured journal (idempotent — dedup by (ts,cid,job)).
    Pairs each `... job J as <name> <email>` line with the following `... job J -> ...` result.
    `text` (a fixture string) and `jobs_by_ids` are injectable for tests."""
    if text is None:
        p = Path(path)
        if not p.is_absolute():
            p = _PATH.parent.parent.parent / path      # repo root / logs/...
        try:
            text = p.read_text(errors="replace")
        except Exception:
            return 0
    parsed: list = []       # (ts, cid, job, mailbox, outcome, detail)
    pending: dict = {}
    for line in text.splitlines():
        m = _LOG_AS_RE.search(line)
        if m:
            pending[(int(m.group(1)), int(m.group(2)))] = m.group(3).strip()
            continue
        m = _LOG_RES_RE.search(line)
        if not m:
            continue
        cid, job = int(m.group(1)), int(m.group(2))
        tsm = _LOG_TS_RE.match(line)
        ts = tsm.group(1) if tsm else ""
        outcome, detail = _outcome_from_log(m.group(3))
        parsed.append((ts, cid, job, pending.get((cid, job), ""), outcome, detail))
    if not parsed:
        return 0
    ids = sorted({j for _, _, j, _, _, _ in parsed})
    if jobs_by_ids is None:
        try:
            from backend.tools.catalog_db import jobs_by_ids as _jbi
            jobs = _jbi(ids) or {}
        except Exception:
            jobs = {}
    else:
        try:
            jobs = jobs_by_ids(ids) or {}
        except Exception:
            jobs = {}
    with _LOCK, _file_lock():
        rows = _events_load()
        seen = {(e.get("ts"), int(e.get("cid", 0)), int(e.get("job_id", 0))) for e in rows}
        added = 0
        for ts, cid, job, mailbox, outcome, detail in parsed:
            key = (ts, cid, job)
            if key in seen:
                continue
            seen.add(key)
            row = jobs.get(job) or jobs.get(str(job)) or {}
            rows.append({"ts": ts, "cid": cid, "job_id": job,
                         "company": row.get("company") or "", "title": row.get("title") or "",
                         "mailbox": mailbox, "outcome": outcome, "detail": detail})
            added += 1
        if added:
            _events_save(rows)
    return added


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="apply-campaigns store maintenance")
    ap.add_argument("--backfill-log", action="store_true",
                    help="parse logs/apply_campaigns.log into the events journal (idempotent)")
    args = ap.parse_args()
    if args.backfill_log:
        n = backfill_events_from_log()
        print(f"backfilled {n} events; journal total = {len(_events_load())}")
    else:
        ap.print_help()
