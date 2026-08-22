"""Region classifier — pure logic, no network/DB. Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_regions.py -q
"""
from backend.applier import regions


def _j(location="", title="", description=""):
    return {"title": title, "location": location, "description": description}


def test_us_only():
    assert regions.classify_regions(_j(location="Remote - US")) == ["US"]
    assert regions.classify_regions(_j(location="Remote (United States)")) == ["US"]


def test_canada():
    assert regions.classify_regions(_j(location="Remote, Canada")) == ["CA"]
    assert regions.classify_regions(_j(location="Toronto, Ontario")) == ["CA"]


def test_north_america_is_us_and_ca():
    assert regions.classify_regions(_j(location="Remote - North America")) == ["US", "CA"]
    assert regions.classify_regions(_j(description="Open to US & Canada")) == ["US", "CA"]


def test_uk():
    assert regions.classify_regions(_j(location="Remote - United Kingdom")) == ["UK"]
    assert regions.classify_regions(_j(location="London, England")) == ["UK"]


def test_worldwide_is_all():
    assert regions.classify_regions(_j(location="Remote - Worldwide")) == ["US", "CA", "UK", "OTHER"]
    assert regions.classify_regions(_j(location="Work from anywhere")) == ["US", "CA", "UK", "OTHER"]


def test_other_only():
    assert regions.classify_regions(_j(location="Remote - EMEA")) == ["OTHER"]
    assert regions.classify_regions(_j(location="Latin America (Remote)")) == ["OTHER"]


def test_multi_us_uk():
    assert regions.classify_regions(_j(location="Remote - US or UK")) == ["US", "UK"]


def test_false_positives_do_not_match_us():
    # "business"/"focus"/"customer" contain the substring "us" — must NOT tag US.
    assert regions.classify_regions(_j(title="Customer Success", description="our business focus")) == []


def test_join_us_in_description_does_not_match_us():
    # ubiquitous "join us"/"contact us" in descriptions must NOT tag US —
    # bare "us"/"uk" are honored only in title/location, never the description.
    j = _j(location="Remote", description="We would love for you to join us — contact us today!")
    assert regions.classify_regions(j) == []


def test_us_in_location_field_matches():
    assert regions.classify_regions(_j(location="US")) == ["US"]


def test_latin_america_is_not_us():
    assert regions.classify_regions(_j(location="Latin America")) == ["OTHER"]


def test_empty_when_no_signal():
    assert regions.classify_regions(_j(location="Remote")) == []


def test_location_country_beats_global_fluff():
    # THE BUG: a foreign-located remote role whose JD says "global/worldwide" used to be
    # tagged US+CA+UK+OTHER (so a US candidate applied to it). Location now restricts.
    assert regions.classify_regions(
        _j(location="Remote - Japan", description="millions of users worldwide")) == ["OTHER"]
    assert regions.classify_regions(
        _j(location="Brazil - Remote", description="as we expand our reach globally")) == ["OTHER"]
    assert regions.classify_regions(
        _j(location="United Kingdom - Remote", description="used in 250 locations globally")) == ["UK"]
    assert regions.classify_regions(
        _j(location="Remote-Hungary", description="navigating global employment compliantly")) == ["OTHER"]
    # Canada-only posting must not also carry US.
    assert regions.classify_regions(_j(location="Canada Wide - Excluding Quebec")) == ["CA"]


def test_global_fluff_alone_is_not_worldwide():
    # bare "global"/"globally"/"worldwide" in the description is NOT an eligibility signal.
    assert regions.classify_regions(_j(location="Remote", description="we serve customers globally")) == []
    assert regions.classify_regions(_j(description="a global leader with users worldwide")) == []
    # but a genuine "work from anywhere" still opens all regions.
    assert regions.classify_regions(
        _j(location="Remote", description="You can work from anywhere in the world.")) == ["US", "CA", "UK", "OTHER"]


def test_source_rule_when_deterministic(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(regions, "_llm_regions", lambda job: called.__setitem__("n", called["n"] + 1) or [])
    out, src = regions.classify_with_source(_j(location="Remote - US"))
    assert out == ["US"] and src == "rule"
    assert called["n"] == 0  # LLM never called when rules resolve


def test_source_llm_on_residue(monkeypatch):
    monkeypatch.setattr(regions, "_llm_regions", lambda job: ["US", "CA"])
    out, src = regions.classify_with_source(_j(location="Remote"))
    assert out == ["US", "CA"] and src == "llm"


def test_source_unknown_when_llm_empty(monkeypatch):
    monkeypatch.setattr(regions, "_llm_regions", lambda job: [])
    out, src = regions.classify_with_source(_j(location="Remote"))
    assert out == [] and src == "unknown"


def test_llm_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(regions, "_llm_regions", lambda job: ["US"])
    out, src = regions.classify_with_source(_j(location="Remote"), use_llm=False)
    assert out == [] and src == "unknown"
