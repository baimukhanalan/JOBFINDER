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


def test_anywhere_in_country_is_that_country_not_worldwide():
    # "Anywhere in the United States" must be US-only, not all-four (a named country beats
    # the bare "anywhere" worldwide token).
    assert regions.classify_regions(_j(location="Anywhere in the United States")) == ["US"]
    assert regions.classify_regions(_j(location="Anywhere in Canada")) == ["CA"]
    assert regions.classify_regions(_j(location="Remote - Worldwide")) == ["US", "CA", "UK", "OTHER"]


def test_georgia_country_vs_us_state():
    # bare "Georgia" (no US context) is the COUNTRY -> OTHER (e.g. a Tbilisi employer)
    assert regions.classify_regions(_j(location="Georgia")) == ["OTHER"]
    # but a US-context "Georgia" stays US
    assert regions.classify_regions(_j(location="Atlanta, Georgia, USA")) == ["US"]
    assert regions.classify_regions(_j(location="Georgia, United States")) == ["US"]
    assert regions.classify_regions(_j(location="Georgia, USA, Remote")) == ["US"]


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


# ---- catalog search: country/nationality term -> region eligibility ----------------
def test_query_eligibility_regions_kazakhstan():
    for term in ("Kazakhstan", "Казахстан", "KZ", "kazakh", "казах", " KZ ", '"Kazakhstan"'):
        assert regions.query_eligibility_regions(term) == ["OTHER"], term


def test_query_eligibility_regions_us_ca_uk():
    for t in ("США", "USA", "United States", "us", "America", "американец"):
        assert regions.query_eligibility_regions(t) == ["US"], t
    for t in ("Canada", "Канада", "canadian"):
        assert regions.query_eligibility_regions(t) == ["CA"], t
    for t in ("UK", "United Kingdom", "Великобритания", "england"):
        assert regions.query_eligibility_regions(t) == ["UK"], t


def test_query_eligibility_regions_foreign_fullmatch_is_other():
    for t in ("Germany", "Japan", "Latin America", "Индия", "europe"):
        assert regions.query_eligibility_regions(t) == ["OTHER"], t


def test_query_eligibility_regions_none_for_non_country():
    # role/company/multi-word/empty queries must NOT be treated as a country -> keep text search
    for t in ("customer support", "engineer", "japan support", "", "  ", "openai", "sales rep"):
        assert regions.query_eligibility_regions(t) is None, repr(t)


# ---- open_anywhere: finer than OTHER (a Kazakhstani can't take a "Remote - India" posting) ------
def _oa(location, description="", title=""):
    return regions.open_anywhere({"location": location, "description": description, "title": title})


def test_open_anywhere_worldwide_and_own_region_locations():
    for loc in ("Anywhere", "Worldwide", "Global - Remote", "Remote, Global", "Remote (Worldwide)",
                "Home based - Worldwide", "Kazakhstan", "Kazakhstan, Astana", "CIS Region", "Central Asia"):
        assert _oa(loc), loc
    # a worldwide location still loses to a role-level pin in the text
    assert not _oa("Remote, Global", "You must be located in the United States for this role.")


def test_open_anywhere_asia_kept_unless_the_text_narrows_it():
    assert _oa("Asia", "Join our distributed team. Flexible hours, fully remote.")
    assert not _oa("Asia", "This role supports our Singapore hub and works APAC hours.")
    assert not _oa("REMOTE - Asia", "Candidates based in the Philippines only.")


def test_open_anywhere_pinned_country_city_or_region_is_closed():
    for loc in ("Remote - India", "Germany", "Germany - Remote", "Hong Kong", "Amsterdam, Netherlands; Remote - Europe",
                "Remote - Europe", "European Union", "EU | Remote", "Remote Poland", "MEXICO", "Remote - Mexico",
                "South Africa - Cape Town", "Singapore", "Remote-Australia", "Taiwan, Taipei", "Remote - LATAM",
                "United States", "London, United Kingdom", "Toronto, Canada", "Remote-EMEA", "Remote in EMEA",
                "Remote, Ireland, EMEA", "APAC", "Remote-APAC", "South East Asia", "Remote (ANZ)", "MENA",
                "Nordics", "Bucharest (Hybrid)", "Anywhere In Philippines"):
        assert not _oa(loc), loc


def test_open_anywhere_bare_remote_or_empty_is_decided_by_the_text():
    # neither a positive nor a negative phrase -> closed (precision over recall)
    assert not _oa("Remote", "A great role on a friendly team.")
    assert not _oa("", "Great remote role. Our users are worldwide.")
    # role-level positives
    assert _oa("Remote", "As a fully remote company, we welcome applicants from almost anywhere.")
    assert _oa("", "This role is remote — you can work from anywhere in the world.")
    assert _oa("Fully Remote", "We hire from anywhere; open to candidates globally.")
    # role-level negatives win over a bare remote
    assert not _oa("Remote", "We are a remote-first company. This role must be based in Latin America.")
    assert not _oa("", "Strictly for Philippines based applicants only. Work from anywhere in the Philippines.")
    assert not _oa("Remote", "Working hours: EST–PST overlap required.")
    assert not _oa("", "Relocate to Lisbon; 2 days a week in the office.")
    assert not _oa("Remote", "Native French speaker required.")
    # a country in the title's trailing segment pins it; our own country opens it
    assert not _oa("", "Remote role.", title="Customer Support Specialist (Bulgaria)")
    assert not _oa("Remote", "Remote role.", title="Sales Development Rep - Philippines")
    assert not _oa("Remote", "Remote role.", title="Math teacher based in Japan")
    assert _oa("", "Remote role.", title="Solutions Specialist (Kazakhstan, remote)")


def test_query_country_aliases():
    for q in ("Kazakhstan", "Казахстан", "KZ", "казахстанец"):
        al = regions.query_country_aliases(q)
        assert "%kazakhstan%" in al and "%казахстан%" in al and "%central asia%" in al, q
    assert regions.query_country_aliases("Uzbekistan") == ["%uzbekistan%"]


def test_open_anywhere_residual_leaks_from_the_recheck():
    # UTC band that excludes UTC+5; APAC/AMER hours; a regional qualifier or internship in the title;
    # a named hiring footprint without Kazakhstan — all closed; a band that CONTAINS +5 stays open
    assert not _oa("Remote, Global", "We are open to candidates across UTC+2 to UTC-8.")
    assert _oa("Remote, Global", "We are open to candidates across UTC-2 to UTC+8.")
    assert not _oa("Remote, Global", "Required location in APAC time zones.")
    assert not _oa("Remote, Global", "This role covers AMER business hours.")
    assert not _oa("Remote, Global", "Remote role.", title="Platform Security Engineer (AMER/APAC)")
    assert not _oa("Asia", "Generic remote role.", title="APAC Controller & Vertical Finance Lead")
    assert not _oa("Asia", "Terms subject to local applicable laws.", title="Binance Accelerator Program - Backend")
    assert not _oa("", "We help businesses build their remote teams in India, Philippines and Sri Lanka. Work from anywhere.")
    assert _oa("", "We hire in over 30 countries — work from anywhere in the world.")
    assert _oa("Remote, Global", "This is a remote position available anywhere in the world!")
