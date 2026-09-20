"""«Сегодня» — a near-real-time snapshot of TODAY's work (since 00:00 server-local time),
served at `/stats/today` and reached from a button on `/stats`.

What it aggregates, all scoped to today:

  * ПОДАЧИ — applications submitted today, per apply lane. The authoritative per-lane
    source is the timestamped apply LOGS each recon driver writes
    (`logs/<lane>_apply.log`): a per-job line `applied job <id> persona=<addr> ->
    confirmed=True|False` (TP/TTEC/Foundever/Sutherland/Alorica), a per-job Workday line,
    or a run-summary `… run done: N jobs, clicked=C, confirmed=F` (Maximus/Kelly) — plus
    the online catalog campaign summary. Parsed by pure helpers below.

  * АССЕССМЕНТЫ — how many assessment INVITES arrived today (Postgres `mail_index`, a
    test-looking inbound today), and how many were PASSED today (the harvester/SHL runner
    log a timestamped `[<persona>] COMPLETED` / `completed banked=` on a real completion).
    Both are broken down by source (the employer pipeline the mail came from), by role and
    counted per candidate.

  * СОБЕСЕДОВАНИЯ / ОФФЕРЫ — today's `interview` / `offer` inbound (distinct persona), each
    enriched with the employer + an APPROXIMATE salary (reusing `interview_priority` /
    `comp_fmt` / `est_comp`: the job's posted comp when the persona resolves to a catalog
    posting, else the role-category median). Plus a PENDING-offer list: personas who passed
    an assessment today on a hire-producing lane and may get an offer soon.

Neutral Russian labels only. The apply-lane / assessment-source labels are EMPLOYER company
names (business data), never our internal tool / ATS / vendor / model stack names.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime

log = logging.getLogger("today_dash")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))   # repo root
_LOGS = os.path.join(_ROOT, "logs")

_TTL = int(os.environ.get("TODAY_TTL", "60"))   # a short cache — «Сегодня» is near-real-time
_CACHE: dict | None = None
_CACHE_AT: float = 0.0
_LOCK = threading.Lock()
_REFRESHING = False


# ---- time window ------------------------------------------------------------------
def day_window(now: datetime | None = None) -> tuple[datetime, int, str]:
    """(local-midnight datetime, its unix ts, 'YYYY-MM-DD' log-date string) for TODAY in the
    server's local timezone. The unix ts bounds DB rows (`date_ts >= since`, date_ts is an
    absolute UTC unix value); the date string matches the apply/harvest logs, which timestamp
    lines in local wall-clock at line start."""
    n = now or datetime.now().astimezone()
    start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, int(start.timestamp()), start.strftime("%Y-%m-%d")


# ---- apply-lane definitions -------------------------------------------------------
# (key, RU/employer label, log filename, parse mode). Labels are EMPLOYER company names
# (business data), never the underlying ATS / vendor stack names.
_APPLY_LANES = [
    ("tp",        "Teleperformance",   "tp_apply.log",        "perjob"),
    ("ttec",      "TTEC",              "taleo_apply.log",     "perjob"),
    ("foundever", "Foundever",         "foundever_apply.log", "perjob"),
    ("sutherland", "Sutherland",       "sr_apply.log",        "perjob"),
    ("alorica",   "Alorica",           "orc_apply.log",       "perjob"),
    ("centene",   "Centene",           "workday_apply.log",   "workday"),
    ("maximus",   "Maximus",           "mh_apply.log",        "summary"),
    ("kelly",     "Kelly",             "kelly_apply.log",     "summary"),
    ("campaign",  "Онлайн-кампания",   "apply_campaigns.log", "campaign"),
]

_LINE_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")
# per-job: "applied job 508 persona=x@ -> confirmed=True"  /  "-> ack=True"
_RE_PERJOB = re.compile(r"applied job (\d+) persona=(\S+)\s*->\s*(?:confirmed|ack)=(True|False)")
# workday per-job: "job 12 persona=x clicked=True confirmed=False error=None"
_RE_WORKDAY = re.compile(r"\bjob (\d+) persona=(\S+) clicked=(\w+) confirmed=(\w+)")
# run summary: "… run done: 8 jobs, submitted=8, confirmed=6"  /  "… clicked=1, confirmed=1"
_RE_SUMMARY = re.compile(r"run done:\s*(\d+)\s+jobs?,\s*(?:clicked|submitted)=(\d+),\s*confirmed=(\d+)")
# online-campaign summary: "apply-campaigns done: 4 applications across 2 campaigns"
_RE_CAMPAIGN = re.compile(r"apply-campaigns done:\s*(\d+)\s+applications")


def parse_apply_log(text: str, mode: str, day: str) -> dict:
    """Pure: count today's submissions from one apply-log body. Returns
    {attempts, confirmed, jobids:set}. `day` = 'YYYY-MM-DD'; only lines whose leading date is
    `day` count. `mode` selects the line grammar (see _APPLY_LANES)."""
    attempts = confirmed = 0
    jobids: set[str] = set()
    for line in (text or "").splitlines():
        md = _LINE_DATE.match(line)
        if not md or md.group(1) != day:
            continue
        if mode == "perjob":
            m = _RE_PERJOB.search(line)
            if m:
                attempts += 1
                jobids.add(m.group(1))
                if m.group(3) == "True":
                    confirmed += 1
        elif mode == "workday":
            m = _RE_WORKDAY.search(line)
            if m:
                attempts += 1
                jobids.add(m.group(1))
                if m.group(4) == "True":
                    confirmed += 1
        elif mode == "summary":
            m = _RE_SUMMARY.search(line)
            if m:
                attempts += int(m.group(1))
                confirmed += int(m.group(3))
        elif mode == "campaign":
            m = _RE_CAMPAIGN.search(line)
            if m:
                n = int(m.group(1))
                attempts += n
                confirmed += n
    return {"attempts": attempts, "confirmed": confirmed, "jobids": jobids}


# ---- harvest / SHL completion parsing (assessments PASSED today) ------------------
# The harvester (Harver/AMCAT/Hallo) and the SHL runner both log a timestamped completion
# tagged with the persona's mailbox localpart: "[first.last1234] COMPLETED …" or
# "[first.last1234] completed banked=110 …". A bracket tag that is NOT a persona (e.g.
# "[harver]", "[taleo]") has no dot+ so is skipped.
_RE_DONE = re.compile(r"\[([^\]]+)\]\s+(?:COMPLETED\b|completed banked=)")
_HARVEST_LOG_GLOBS = ("harvest_*.log", "parallel_taleo_drain.log", "shl_assess.log",
                      "shl_watch.log")


def parse_harvest_completions(text: str, day: str) -> set[str]:
    """Pure: the set of persona localparts marked COMPLETED today in one harvester/SHL log
    body. Deduped; non-persona bracket tags (no '.') dropped."""
    out: set[str] = set()
    for line in (text or "").splitlines():
        md = _LINE_DATE.match(line)
        if not md or md.group(1) != day:
            continue
        m = _RE_DONE.search(line)
        if m:
            tag = m.group(1).strip().lower()
            if "." in tag and "@" not in tag and " " not in tag:
                out.add(tag)
    return out


# ---- sender → employer pipeline ---------------------------------------------------
# Maps an inbound assessment/interview/offer sender to the EMPLOYER pipeline it belongs to
# (a company name = business data). shl.com carries BOTH Sutherland and TP flows, split by
# subject. Everything unmatched → «Другие».
def sender_source(from_email: str, subject: str = "") -> tuple[str, str]:
    fe = (from_email or "").lower()
    subj = (subject or "").lower()
    if "shl.com" in fe or "talentcentral" in fe:
        if "sutherland" in subj:
            return "sutherland", "Sutherland"
        if "tp assessment" in subj or "teleperformance" in subj:
            return "tp", "Teleperformance"
        return "sutherland", "Sutherland"
    checks = [
        (("maximus.com",), "maximus", "Maximus"),
        (("ttec.com", "teletech", "harver.com"), "ttec", "TTEC"),
        (("talent.icims.com", "teleperformance", "hallo.ai"), "tp", "Teleperformance"),
        (("workday.com", "myworkday", "centene"), "centene", "Centene"),
        (("kellyservices.com", "mykelly"), "kelly", "Kelly"),
        (("smartrecruiters.com",), "sutherland", "Sutherland"),
        (("foundever.com", "successfactors"), "foundever", "Foundever"),
        (("alorica", "oraclecloud", "jobcredits"), "alorica", "Alorica"),
    ]
    for needles, key, label in checks:
        if any(nd in fe for nd in needles):
            return key, label
    return "other", "Другие"


# The lanes whose assessment PASS is followed by a human interview / on-the-spot hire — so a
# candidate who passed today «может получить оффер». (All current mass-hiring lanes qualify;
# TP is the fastest.)
_OFFER_PRODUCING = {"tp", "ttec", "maximus", "sutherland", "foundever", "alorica", "kelly", "centene"}


# ---- role labels ------------------------------------------------------------------
_ROLE_RU = {
    "Engineering": "Инженерия", "Data & ML": "Данные и ML", "Product": "Продукт",
    "Design": "Дизайн", "Customer Support & Success": "Поддержка клиентов",
    "Sales": "Продажи", "Marketing": "Маркетинг", "Operations": "Операции",
    "Finance & Accounting": "Финансы", "People & HR": "HR", "Legal": "Юристы",
    "Healthcare": "Медицина", "Other": "Прочее",
}


def role_label(cat: str | None) -> str:
    return _ROLE_RU.get(cat or "", cat or "Прочее")


def _role_from_subject(subject: str) -> str | None:
    """Approximate role_category from an email subject (the durable signal when the persona's
    prefill artifact — hence its jobid — is gone or was a mass-hiring apply). None → unknown."""
    try:
        from backend.applier import role_category
        cat, _ = role_category.classify_role(subject or "", "")
        return cat if (cat and cat != "Other") else None
    except Exception:
        return None


def _salary_for(role_category: str | None, job: dict | None) -> tuple[str, bool]:
    """(compact salary label, estimated?) for a card. The job's posted/researched comp when
    it resolves to a catalog posting, else the role-category median — reusing the exact
    `interview_priority` / `comp_fmt` / `est_comp` logic the operator surfaces elsewhere."""
    from backend.tools import comp_fmt
    from backend.tools import interview_priority as ip
    if job and comp_fmt.has_comp(job):
        return ip.salary_label(job), False
    try:
        from backend.applier import est_comp
        est = est_comp.estimate(role_category, ["US"])
        lbl = ip.salary_label(est)
        return (("~" + lbl if lbl and not lbl.startswith("~") else lbl), True)
    except Exception:
        return "", True


# ---- live aggregation -------------------------------------------------------------
def _read(fname: str) -> str:
    try:
        with open(os.path.join(_LOGS, fname), errors="ignore") as fh:
            return fh.read()
    except Exception:
        return ""


def _submissions(day: str) -> dict:
    lanes = []
    tot_a = tot_c = 0
    for key, label, fname, mode in _APPLY_LANES:
        r = parse_apply_log(_read(fname), mode, day)
        if r["attempts"] or r["confirmed"]:
            lanes.append({"key": key, "label": label,
                          "attempts": r["attempts"], "confirmed": r["confirmed"]})
            tot_a += r["attempts"]
            tot_c += r["confirmed"]
    lanes.sort(key=lambda x: (x["confirmed"], x["attempts"]), reverse=True)
    return {"total_confirmed": tot_c, "total_attempts": tot_a, "lanes": lanes}


def _solved_today(day: str) -> set[str]:
    """Persona mailboxes (@takhet.com) whose assessment was PASSED today, unioned across the
    harvester + SHL runner logs. Best-effort; a missing log is just skipped."""
    locals_: set[str] = set()
    seen_files: set[str] = set()
    for pat in _HARVEST_LOG_GLOBS:
        for path in glob.glob(os.path.join(_LOGS, pat)):
            if path in seen_files:
                continue
            seen_files.add(path)
            try:
                with open(path, errors="ignore") as fh:
                    locals_ |= parse_harvest_completions(fh.read(), day)
            except Exception:
                continue
    return {lp + "@takhet.com" for lp in locals_}


def _assessment_invites(cur, since: int) -> dict:
    """Today's assessment INVITE mail (a test-looking inbound), by source pipeline and by
    role. `msgs` counts messages; `personas` counts distinct mailboxes."""
    from backend.tools import mail_db
    cur.execute(
        f"""SELECT from_email, subject, mailbox FROM mail_index
            WHERE NOT outbound AND kind='action_needed' AND date_ts >= %s
              AND {mail_db._TEST_SUBJECT_SQL}""", (since,))
    by_src_msgs: dict = defaultdict(int)
    by_src_pers: dict = defaultdict(set)
    by_role_pers: dict = defaultdict(set)
    src_label: dict = {}
    total_pers: set = set()
    for fe, subj, mb in cur.fetchall():
        skey, slabel = sender_source(fe, subj)
        src_label[skey] = slabel
        by_src_msgs[skey] += 1
        by_src_pers[skey].add(mb)
        total_pers.add(mb)
        by_role_pers[role_label(_role_from_subject(subj))].add(mb)
    by_source = sorted(
        ({"label": src_label[k], "msgs": by_src_msgs[k], "personas": len(by_src_pers[k])}
         for k in by_src_msgs), key=lambda x: x["personas"], reverse=True)
    by_role = sorted(({"label": k, "personas": len(v)} for k, v in by_role_pers.items()),
                     key=lambda x: x["personas"], reverse=True)
    return {"total_msgs": sum(by_src_msgs.values()), "total_personas": len(total_pers),
            "by_source": by_source, "by_role": by_role}


def _enrich_source_role(cur, mailboxes: list[str]) -> dict:
    """{mailbox: {source_key, source_label, role_category}} from each mailbox's most recent
    inbound mail (sender → employer pipeline; subject → role). One batched query."""
    out: dict = {}
    if not mailboxes:
        return out
    cur.execute(
        "SELECT DISTINCT ON (mailbox) mailbox, from_email, subject FROM mail_index "
        "WHERE mailbox = ANY(%s) AND NOT outbound "
        "ORDER BY mailbox, date_ts DESC, path_hash DESC", (list(mailboxes),))
    for mb, fe, subj in cur.fetchall():
        skey, slabel = sender_source(fe, subj)
        out[mb] = {"source_key": skey, "source_label": slabel,
                   "role_category": _role_from_subject(subj)}
    return out


def _assessments_solved(cur, day: str) -> dict:
    """Assessments PASSED today: total + by source + by role + per-candidate cards (with an
    approximate salary, so the operator sees who «может получить оффер» and at roughly what pay)."""
    mboxes = sorted(_solved_today(day))
    meta = _enrich_source_role(cur, mboxes)
    by_src: dict = defaultdict(int)
    by_role: dict = defaultdict(int)
    src_label: dict = {}
    cards = []
    for mb in mboxes:
        m = meta.get(mb, {})
        skey = m.get("source_key", "other")
        slabel = m.get("source_label", "Другие")
        cat = m.get("role_category")
        src_label[skey] = slabel
        by_src[skey] += 1
        by_role[role_label(cat)] += 1
        sal, est = _salary_for(cat, None)
        cards.append({"email": mb, "source_key": skey, "source": slabel,
                      "role": role_label(cat), "salary_label": sal, "salary_estimated": est})
    return {
        "total": len(mboxes),
        "by_source": sorted(({"label": src_label[k], "n": by_src[k]} for k in by_src),
                            key=lambda x: x["n"], reverse=True),
        "by_role": sorted(({"label": k, "n": v} for k, v in by_role.items()),
                          key=lambda x: x["n"], reverse=True),
        "cards": cards,
    }


def _stage_cards(cur, since: int, kind: str) -> list[dict]:
    """Today's inbound rows of one kind (interview|offer), one card per DISTINCT persona,
    enriched with employer + role + an approximate salary (posted comp when the persona
    resolves to a catalog posting, else the role-category median)."""
    cur.execute(
        "SELECT DISTINCT ON (mailbox) mailbox, from_email, subject, snippet, date_ts, path_hash "
        "FROM mail_index WHERE NOT outbound AND kind=%s AND date_ts >= %s "
        "ORDER BY mailbox, date_ts DESC, path_hash DESC", (kind, since))
    rows = [{"mailbox": mb, "from_email": fe, "subject": subj or "", "snippet": sn or "",
             "date_ts": ts, "path_hash": ph}
            for mb, fe, subj, sn, ts, ph in cur.fetchall()]
    if not rows:
        return []
    # Reuse the interview-priority enrichment (role_category + salary_value/label from the
    # persona's job, or the role-category median) — the same logic the «Собес» surface uses.
    try:
        from backend.tools import interview_priority as ip
        ip.enrich_interview_groups(rows, hash_key="path_hash")
    except Exception:
        pass
    # resolve the posted-comp job (for personas that DO map to a catalog posting)
    jobs: dict = {}
    try:
        from backend.tools import catalog_db
        ids = sorted({int(r["jobid"]) for r in rows
                      if r.get("jobid") and str(r.get("jobid")).isdigit()})
        if ids:
            jobs = catalog_db.jobs_by_ids(ids)
    except Exception:
        jobs = {}
    cards = []
    for r in rows:
        jid = r.get("jobid")
        job = jobs.get(int(jid)) if (jid and str(jid).isdigit()) else None
        cat = r.get("role_category")
        skey, slabel = sender_source(r["from_email"], r["subject"])
        company = (job or {}).get("company") or slabel
        # prefer the enrichment's salary_label (already posted-or-median); recompute if blank
        sal = r.get("salary_label") or ""
        est = r.get("salary_estimated", True)
        if not sal:
            sal, est = _salary_for(cat, job)
        cards.append({
            "email": r["mailbox"], "company": company, "source": slabel,
            "role": role_label(cat), "direction": r.get("direction", "other"),
            "salary_label": sal, "salary_estimated": bool(est),
            "subject": (r["subject"] or "")[:80],
        })
    return cards


def compute_today(now: datetime | None = None) -> dict:
    """The full «Сегодня» blob. Cheap (a handful of small log reads + a few DB queries) —
    cached briefly so the surface stays near-real-time."""
    from backend.tools import mail_db
    t0 = time.time()
    start, since, day = day_window(now)
    submissions = _submissions(day)

    invites = {"total_msgs": 0, "total_personas": 0, "by_source": [], "by_role": []}
    solved = {"total": 0, "by_source": [], "by_role": [], "cards": []}
    interviews: list = []
    offers: list = []
    try:
        with mail_db._cur(dict_rows=False) as cur:
            invites = _assessment_invites(cur, since)
            solved = _assessments_solved(cur, day)
            interviews = _stage_cards(cur, since, "interview")
            offers = _stage_cards(cur, since, "offer")
    except Exception as e:
        log.warning("today_dash: DB section failed: %s", e)

    # PENDING offers = passed-an-assessment-today on a hire-producing lane → an offer may come.
    pending_cards = [c for c in solved["cards"] if c.get("source_key") in _OFFER_PRODUCING]
    pending_by_src: dict = defaultdict(int)
    for c in pending_cards:
        pending_by_src[c["source"]] += 1

    # «Кандидаты с оффером/собеседованием»: the offer + interview cards (company + salary).
    candidates = ([{**c, "stage": "offer"} for c in offers]
                  + [{**c, "stage": "interview"} for c in interviews])

    blob = {
        "generated_at": int(time.time()),
        "took_ms": int((time.time() - t0) * 1000),
        "day_label": start.strftime("%d.%m.%Y"),
        "submissions": submissions,
        "assessments": {"invites": invites, "solved": solved},
        "interviews": {"total": len(interviews), "cards": interviews},
        "offers": {
            "arrived": {"total": len(offers), "cards": offers},
            "pending": {
                "total": len(pending_cards),
                "by_source": sorted(({"label": k, "n": v} for k, v in pending_by_src.items()),
                                    key=lambda x: x["n"], reverse=True),
                "cards": pending_cards,
            },
        },
        "candidates": candidates,
    }
    log.info("today computed in %sms: subs=%s invites=%s solved=%s iv=%s off=%s",
             blob["took_ms"], submissions["total_confirmed"], invites["total_personas"],
             solved["total"], len(interviews), len(offers))
    return blob


def get_today(force: bool = False) -> dict:
    """Serve the cached blob; recompute synchronously if cold or forced, refresh in the
    background when stale (same pattern as tools.stats)."""
    global _CACHE, _CACHE_AT, _REFRESHING
    now = time.time()
    if _CACHE is not None and not force and (now - _CACHE_AT) < _TTL:
        return _CACHE
    if _CACHE is None or force:
        with _LOCK:
            if _CACHE is None or force:
                _CACHE = compute_today()
                _CACHE_AT = time.time()
        return _CACHE
    with _LOCK:
        if _REFRESHING:
            return _CACHE
        _REFRESHING = True
    threading.Thread(target=_bg_refresh, daemon=True).start()
    return _CACHE


def _bg_refresh() -> None:
    global _CACHE, _CACHE_AT, _REFRESHING
    try:
        blob = compute_today()
        with _LOCK:
            _CACHE = blob
            _CACHE_AT = time.time()
    except Exception as e:
        log.warning("today bg refresh failed: %s", e)
    finally:
        _REFRESHING = False
