"""Pure-helper tests for the «Сегодня» dashboard (`today_dash`) — the today-window filter,
the per-lane apply-log parse, the harvest-completion parse and the sender→employer map. No
network, no DB, no filesystem. Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_today_dashboard.py -q
"""
from datetime import datetime, timedelta

from backend.tools import today_dash as t


# ---- day_window -------------------------------------------------------------------
def test_day_window_is_local_midnight():
    now = datetime(2026, 9, 20, 15, 42, 7).astimezone()
    start, since, day = t.day_window(now)
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert day == "2026-09-20"
    assert since == int(start.timestamp())
    assert start.date() == now.date()


# ---- parse_apply_log: per-job lanes (TP/TTEC/Foundever/…) -------------------------
_PERJOB_LOG = """\
2026-09-20 14:17:18,256 applied job 508 persona=a@takhet.com -> confirmed=True
2026-09-20 14:27:11,868 applied job 509 persona=b@takhet.com -> confirmed=False
2026-09-20 14:31:40,273 applied job 510 persona=c@takhet.com -> confirmed=True
2026-09-20 14:46:58,553 taleo apply run done: 3 jobs, submitted=3, confirmed=2
2026-09-19 09:00:00,000 applied job 999 persona=old@takhet.com -> confirmed=True
"""


def test_parse_perjob_counts_today_only():
    r = t.parse_apply_log(_PERJOB_LOG, "perjob", "2026-09-20")
    assert r["attempts"] == 3          # 3 per-job lines today; the summary line is NOT double-counted
    assert r["confirmed"] == 2         # two confirmed=True today (yesterday's line excluded)
    assert r["jobids"] == {"508", "509", "510"}


def test_parse_perjob_handles_ack_variant():
    log = ("2026-09-20 13:40:17,141 applied job 1608 persona=x@ -> ack=False\n"
           "2026-09-20 13:48:36,667 applied job 12007 persona=y@ -> ack=True\n")
    r = t.parse_apply_log(log, "perjob", "2026-09-20")
    assert r["attempts"] == 2 and r["confirmed"] == 1


def test_parse_workday_perjob():
    log = ("2026-09-20 16:39:14,266 Filled '[id=x]' = 'noise'\n"
           "2026-09-20 16:39:21,917 job 77 persona=z@ clicked=True confirmed=True error=None\n"
           "2026-09-20 16:40:00,000 job 78 persona=q@ clicked=True confirmed=False error=None\n")
    r = t.parse_apply_log(log, "workday", "2026-09-20")
    assert r["attempts"] == 2 and r["confirmed"] == 1


def test_parse_summary_lane_sums_runs():
    log = ("2026-09-20 05:00:00,000 apply run done: 15 jobs, clicked=6, confirmed=2\n"
           "2026-09-20 10:00:00,000 apply run done: 12 jobs, clicked=4, confirmed=1\n"
           "2026-09-19 05:00:00,000 apply run done: 20 jobs, clicked=9, confirmed=5\n")
    r = t.parse_apply_log(log, "summary", "2026-09-20")
    assert r["attempts"] == 27 and r["confirmed"] == 3      # yesterday's run excluded


def test_parse_kelly_summary_with_errors_tail():
    log = "2026-09-20 12:34:06,400 Kelly apply run done: 7 jobs, clicked=1, confirmed=1, errors=0\n"
    r = t.parse_apply_log(log, "summary", "2026-09-20")
    assert r["attempts"] == 7 and r["confirmed"] == 1


def test_parse_campaign_summary():
    log = ("2026-09-20 13:08:02,289 apply-campaigns done: 4 applications across 2 campaigns\n"
           "2026-09-20 07:08:02,492 apply-campaigns done: 0 applications across 0 campaigns\n")
    r = t.parse_apply_log(log, "campaign", "2026-09-20")
    assert r["attempts"] == 4 and r["confirmed"] == 4


def test_parse_empty_log():
    r = t.parse_apply_log("", "perjob", "2026-09-20")
    assert r == {"attempts": 0, "confirmed": 0, "jobids": set()}


# ---- parse_harvest_completions ----------------------------------------------------
_HARVEST_LOG = """\
2026-09-20 14:26:11,197 [vivian.harlow4359] completed banked=109 by_type={'x':1} note=completed
2026-09-20 14:51:59,737 [ella.reed1025] COMPLETED — marked assessment done
2026-09-20 14:51:59,737 [ella.reed1025] completed banked=110 by_type={'x':1} note=completed
2026-09-20 14:29:44,506 [harver] PERS probe: is_pers=True n=6
2026-09-19 10:00:00,000 [old.persona1] COMPLETED — marked assessment done
2026-09-20 15:00:00,000 [name.surname9] COMPLETED
"""


def test_parse_harvest_dedupes_and_filters():
    got = t.parse_harvest_completions(_HARVEST_LOG, "2026-09-20")
    # ella.reed1025 appears on two lines → counted once; vivian + name.surname9 today; harver
    # is not a persona (no dot pattern → it HAS a dot? "harver" has none) and old.persona1 is
    # yesterday → excluded.
    assert got == {"vivian.harlow4359", "ella.reed1025", "name.surname9"}


def test_parse_harvest_excludes_non_persona_tag():
    # a bracket tag without a '.' is a platform label, never a persona
    log = "2026-09-20 01:00:00,000 [taleo] session start\n2026-09-20 01:01:00,000 [amcat] COMPLETED\n"
    assert t.parse_harvest_completions(log, "2026-09-20") == set()


# ---- sender_source (employer pipeline map) ----------------------------------------
def test_sender_source_employers():
    assert t.sender_source("careers@maximus.com", "x")[1] == "Maximus"
    assert t.sender_source("jobopportunities@ttec.com", "Required Assessments")[1] == "TTEC"
    assert t.sender_source("noreply@harver.com", "y")[1] == "TTEC"
    assert t.sender_source("teleperformance+autoreply@talent.icims.com", "z")[1] == "Teleperformance"
    assert t.sender_source("support@hallo.ai", "assessment")[1] == "Teleperformance"
    assert t.sender_source("centene@myworkday.com", "w")[1] == "Centene"
    assert t.sender_source("noreply_mykelly@kellyservices.com", "w")[1] == "Kelly"


def test_sender_source_shl_split_by_subject():
    # shl.com serves BOTH Sutherland and TP flows — the subject disambiguates
    assert t.sender_source("talentcentral@shl.com", "Your Sutherland assessment invitation")[1] == "Sutherland"
    assert t.sender_source("talentcentral@shl.com", "TP Assessment - Test Login Details")[1] == "Teleperformance"


def test_sender_source_unknown_is_other():
    key, label = t.sender_source("recruiter@some-startup.com", "Interview")
    assert (key, label) == ("other", "Другие")


# ---- role_label -------------------------------------------------------------------
def test_role_label_maps_and_falls_back():
    assert t.role_label("Engineering") == "Инженерия"
    assert t.role_label("Customer Support & Success") == "Поддержка клиентов"
    assert t.role_label(None) == "Прочее"


# ---- window isolation -------------------------------------------------------------
def test_yesterdays_lines_never_leak_across_window():
    # a defensive end-to-end: only the target day's lines are ever counted
    y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    log = f"{y} 10:00:00,000 applied job 1 persona=a@ -> confirmed=True\n"
    assert t.parse_apply_log(log, "perjob", datetime.now().strftime("%Y-%m-%d"))["confirmed"] == 0
