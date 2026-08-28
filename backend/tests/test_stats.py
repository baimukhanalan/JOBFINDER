"""Pure aggregation test for backend.tools.stats — no DB, no filesystem.

Feeds compute_stats() known fixtures by monkeypatching its two data sources and
asserts the per-company / funnel / ATS math. The unit of account is the jobid
(a distinct posting), so retries under fresh personas must NOT inflate `applied`.
"""
from backend.tools import stats


def _install(monkeypatch):
    # acme: j1, j2, j3 (j1 was RETRIED under two personas a1 + a1b — must count once).
    # globex: j4, j5.
    jobid_company = {"j1": "acme", "j2": "acme", "j3": "acme",
                     "j4": "globex", "j5": "globex"}
    jobid_emails = {
        "j1": {"a1@x", "a1b@x"},   # retried: two personas, one is interview
        "j2": {"a2@x"},
        "j3": {"a3@x"},
        "j4": {"g1@x"},
        "j5": {"g2@x"},
    }
    display = {"acme": "Acme", "globex": "Globex"}
    app_emails = {"a1@x", "a1b@x", "a2@x", "a3@x", "g1@x", "g2@x"}
    scan = {"jobid_company": jobid_company, "jobid_emails": jobid_emails,
            "display": display, "attempts": 6, "app_emails": app_emails}

    # a1@x got interview, a1b@x only ack -> job j1 best outcome = interview
    best = {"a1@x": "interview", "a1b@x": "ack", "a2@x": "rejection",
            "a3@x": "ack", "g1@x": "interview", "g2@x": "offer"}
    inbound = [(1_787_000_000, "interview", "a1@x"), (1_787_000_000, "ack", "a3@x"),
               (1_787_100_000, "rejection", "a2@x"),
               (1_787_100_000, "ack", "someone-else@x")]  # not one of ours -> excluded
    jid_ats = {"j1": "greenhouse", "j2": "greenhouse", "j3": "ashby",
               "j4": "greenhouse", "j5": "lever"}
    jid_region = {"j1": "US", "j2": "US", "j3": "OTHER", "j4": "US", "j5": "CA"}

    monkeypatch.setattr(stats, "_scan_applications", lambda: scan)
    monkeypatch.setattr(stats, "_mail_outcomes", lambda: (best, inbound))
    monkeypatch.setattr(stats, "bulk_submitted", lambda: {"j1", "j2", "j4"})
    monkeypatch.setattr(stats, "_catalog_dims", lambda jids: (jid_ats, jid_region))


def test_company_aggregation_dedupes_retries(monkeypatch):
    _install(monkeypatch)
    b = stats.compute_stats()
    by = {c["key"]: c for c in b["companies"]}

    acme = by["acme"]
    assert acme["name"] == "Acme"
    assert acme["applied"] == 3          # j1 counted ONCE despite 2 personas
    assert acme["submitted"] == 2        # j1, j2
    assert acme["replied"] == 3          # j1, j2, j3 all have inbound
    assert acme["interview"] == 1        # j1 (best over a1/a1b = interview)
    assert acme["rejection"] == 1        # j2
    assert acme["ack"] == 1              # j3
    assert acme["offer"] == 0
    assert acme["reply_rate"] == 100.0
    assert acme["interview_rate"] == 33.3

    glx = by["globex"]
    assert glx["applied"] == 2
    assert glx["interview"] == 1
    assert glx["offer"] == 1


def test_totals_and_attempts(monkeypatch):
    _install(monkeypatch)
    b = stats.compute_stats()
    t = b["totals"]
    assert t["applied"] == 5             # distinct jobs, not 6 personas
    assert t["attempts"] == 6            # fill attempts incl. the retry
    assert t["submitted"] == 3
    assert t["replied"] == 5
    assert t["interview"] == 2
    assert t["offer"] == 1
    assert t["rejection"] == 1
    assert t["companies"] == 2
    assert sum(c["interview"] for c in b["companies"]) == t["interview"]
    assert t["interview_rate"] == 40.0   # 2/5


def test_ats_region_share_the_base(monkeypatch):
    _install(monkeypatch)
    b = stats.compute_stats()
    # ATS/region breakdowns must sum to the same base as totals.applied
    assert sum(r["applied"] for r in b["ats"]) == b["totals"]["applied"]
    assert sum(r["applied"] for r in b["regions"]) == b["totals"]["applied"]
    ats = {r["ats"]: r for r in b["ats"]}
    assert ats["greenhouse"]["applied"] == 3   # j1, j2, j4
    assert ats["greenhouse"]["interview"] == 2  # j1, j4
    assert ats["ashby"]["applied"] == 1
    assert ats["lever"]["applied"] == 1


def test_trend_scoped_to_our_applications(monkeypatch):
    _install(monkeypatch)
    b = stats.compute_stats()
    # the stray "someone-else@x" ack is excluded; our 3 in-scope inbounds remain
    total = sum(d["total"] for d in b["trend"])
    assert total == 3


def test_sorted_by_interview_then_applied(monkeypatch):
    _install(monkeypatch)
    b = stats.compute_stats()
    assert [c["key"] for c in b["companies"]] == ["acme", "globex"]


def test_pure_helpers():
    assert stats._norm_company("  GitLab ") == "gitlab"
    assert stats._rank("offer") > stats._rank("interview") > stats._rank("rejection")
    assert stats._rank("nonsense") == -1
    d = stats._day_start(1_787_000_000)
    assert d <= 1_787_000_000 and stats._day_start(d) == d
    # prettify lowercase slugs but keep existing mixed-case names intact
    assert stats._pretty("affirm") == "Affirm"
    assert stats._pretty("nebius") == "Nebius"
    assert stats._pretty("GitLab") == "GitLab"
    assert stats._pretty("OpenAI") == "OpenAI"
    assert stats._pretty("1Password") == "1Password"
