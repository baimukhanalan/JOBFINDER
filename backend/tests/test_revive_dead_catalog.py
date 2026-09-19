"""revive_dead_catalog verdict + key logic — pure, no DB/network. Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_revive_dead_catalog.py -q
"""
import datetime

from backend.tools import revive_dead_catalog as rv

NOW = datetime.datetime(2026, 9, 19, 12, 0, tzinfo=datetime.timezone.utc)


def _ls(days_ago):
    return NOW - datetime.timedelta(days=days_ago)


# ---- match_key -----------------------------------------------------------------
def test_match_key_greenhouse_numeric():
    row = {"ats": "greenhouse", "external_id": "8687129002",
           "url": "https://job-boards.greenhouse.io/gympass/jobs/8687129002"}
    assert rv.match_key(row) == "8687129002"


def test_match_key_greenhouse_sha1_fallback_reads_url():
    # external_id is a collector sha1 fallback -> gh_jid comes from the url
    row = {"ats": "greenhouse", "external_id": "a1b2c3d4e5f6a7b8",
           "url": "https://www.nextiva.com/company/careers-listing?gh_jid=8686725002"}
    assert rv.match_key(row) == "8686725002"


def test_match_key_ashby_uuid():
    row = {"ats": "ashby", "external_id": "9006e97d-4451-487e-ab63-0e84103870f1",
           "url": "https://jobs.ashbyhq.com/salmon-group/9006e97d-4451-487e-ab63-0e84103870f1/application"}
    assert rv.match_key(row) == "9006e97d-4451-487e-ab63-0e84103870f1"


# ---- classify ------------------------------------------------------------------
def test_on_board_revives():
    v, d = rv.classify("X", rv.OK, {"X", "Y"}, _ls(20), NOW, fresh_days=2)
    assert (v, d) == ("live", "on_board")


def test_gone_from_board_stays_dead():
    v, d = rv.classify("X", rv.OK, {"Y", "Z"}, _ls(1), NOW, fresh_days=2)
    assert (v, d) == ("dead", "gone_from_board")


def test_board_404_stays_dead_even_if_recent():
    v, d = rv.classify("X", rv.GONE_404, set(), _ls(0), NOW, fresh_days=2)
    assert (v, d) == ("dead", "board_404")


def test_board_error_recent_collect_revives():
    # board fetch failed, but the nightly collector re-saw the row yesterday -> proof of life
    v, d = rv.classify("X", rv.ERROR, set(), _ls(1), NOW, fresh_days=2)
    assert (v, d) == ("live", "recent_collect")


def test_board_error_stale_stays_dead():
    v, d = rv.classify("X", rv.ERROR, set(), _ls(30), NOW, fresh_days=2)
    assert (v, d) == ("dead", "board_error")


def test_board_error_no_last_seen_stays_dead():
    v, d = rv.classify("X", rv.ERROR, set(), None, NOW, fresh_days=2)
    assert (v, d) == ("dead", "board_error")


def test_naive_last_seen_treated_as_utc():
    naive = datetime.datetime(2026, 9, 19, 0, 0)  # tz-naive, 12h ago
    v, d = rv.classify("X", rv.ERROR, set(), naive, NOW, fresh_days=2)
    assert v == "live"
