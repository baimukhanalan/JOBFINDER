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
