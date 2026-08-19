"""Answer cache v2: keyed (profile, niche, question) — no cross-person reuse."""
import backend.answer_cache as ac


def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "DB_PATH", str(tmp_path / "cache.db"))


def test_profiles_do_not_share_answers(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    q = "Why do you want to work at Acme?"
    ac.put_many({q: "Because I love helping Acme customers."}, "Acme",
                profile="michael", niche="bpo-voice-qa")
    assert ac.get_many([q], "Acme", profile="kate", niche="bpo-voice-qa") == {}
    got = ac.get_many([q], "Acme", profile="michael", niche="bpo-voice-qa")
    assert got == {q: "Because I love helping Acme customers."}


def test_niches_do_not_share_answers(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    q = "How many years of experience do you have?"
    ac.put_many({q: "12+ years."}, "", profile="michael", niche="chat-email-async")
    assert ac.get_many([q], "", profile="michael", niche="bpo-voice-qa") == {}
    assert ac.get_many([q], "", profile="michael", niche="chat-email-async") == {q: "12+ years."}


def test_company_genericized_across_companies(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    q1 = "Why do you want to work at Acme?"
    ac.put_many({q1: "Acme's product impressed me."}, "Acme", profile="m", niche="")
    q2 = "Why do you want to work at Zapier?"
    got = ac.get_many([q2], "Zapier", profile="m", niche="")
    assert got == {q2: "Zapier's product impressed me."}


def test_default_profile_separate_from_named(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    q = "Are you comfortable with rotating schedules?"
    ac.put_many({q: "Yes, fully flexible."}, "")  # legacy caller, no profile kwarg
    assert ac.get_many([q], "") == {q: "Yes, fully flexible."}
    assert ac.get_many([q], "", profile="michael") == {}


def test_stats(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    ac.put_many({"Question one is long enough?": "A."}, "", profile="m", niche="")
    s = ac.stats()
    assert s["cached_questions"] == 1


def test_company_substring_not_replaced(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    assert "pine<co>" not in ac.normalize("Do you like pineapple?", "Apple")
    assert ac.normalize("Why work at Apple?", "Apple") == "why work at <co>"


def test_db_path_switch_recreates_schema(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    ac.put_many({"A long enough question one?": "A."}, "", profile="m")
    monkeypatch.setattr(ac, "DB_PATH", str(tmp_path / "other.db"))
    ac.put_many({"A long enough question two?": "B."}, "", profile="m")  # must not raise
    assert ac.get_many(["A long enough question two?"], "", profile="m")
