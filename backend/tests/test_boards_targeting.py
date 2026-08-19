"""Targeting tests for boards.py: JD cleaning, word-boundary fit, geo/language
eligibility, score-floor ranking, and the employer/aggregator slug gates.

Run: PYTHONPATH=. python3 -m pytest backend/tests/test_boards_targeting.py -q

Pure-logic tests: no network, no DB, no browser.
"""
import html
import os

from backend.applier import boards
from backend.services.tailor import keywords as kw

# Greenhouse ships the `content` field HTML-ESCAPED (escaped HTML of the real JD).
_RAW_GH_HTML = (
    '<p><strong class="intro">Customer Support Specialist</strong>&nbsp;(Remote)</p>'
    '<ul><li><span class="req">Zendesk &amp; Intercom experience required.</span></li>'
    "<li>Handle Tier 1 troubleshooting over live chat and email.</li></ul>"
)
_ESCAPED_GH_CONTENT = html.escape(_RAW_GH_HTML)


# 1) Cleaning order: unescape BEFORE tag-strip, so escaped tags never survive as
#    keyword tokens ("nbsp"/"strong"/"class" polluted missing_keywords in reports).
def test_escaped_greenhouse_html_produces_no_tag_tokens():
    cleaned = boards._strip_html(_ESCAPED_GH_CONTENT)

    import re
    assert "<" not in cleaned and ">" not in cleaned, f"tags survived: {cleaned!r}"
    # No named entities left (a bare '&' from a decoded &amp; is correct output).
    assert not re.search(r"&[a-z]+;", cleaned), f"entities survived: {cleaned!r}"

    tokens = kw.extract_jd_keywords(cleaned)
    junk = {"nbsp", "strong", "class", "span", "intro", "amp", "href", "style"}
    assert not junk & set(tokens), f"tag junk leaked into keywords: {sorted(junk & set(tokens))}"
    # The real content is still extracted.
    assert "customer support" in tokens
    assert "zendesk" in tokens


def test_strip_html_handles_double_escaped_nbsp():
    # &amp;nbsp; -> &nbsp; after the first unescape; must end as plain whitespace.
    cleaned = boards._strip_html("Great&amp;nbsp;benefits and&nbsp;pay")
    assert "nbsp" not in cleaned
    assert cleaned == "Great benefits and pay"


# 2) Word boundaries: 'chat' must not match 'ChatGPT' (this queued an OpenAI
#    Android Engineer role), and 'sales' must not swallow 'Salesforce'.
def test_chat_keyword_does_not_match_chatgpt_title():
    assert not boards.is_fit({"title": "Android Engineer, ChatGPT"})
    assert not boards._kw_match("android engineer, chatgpt", ["chat"])
    # ...while real chat roles still match.
    assert boards.is_fit({"title": "Live Chat Support Agent"})


def test_wrong_discipline_titles_excluded():
    for title in ("iOS Engineer, Customer Products",
                  "Machine Learning Engineer, Support Automation",
                  "Data Scientist, Community Support",
                  "VP of Customer Success"):
        assert not boards.is_fit({"title": title}), f"{title!r} should be excluded"


def test_support_engineer_family_stays_desired():
    # Deliberate volume — see the _FIT_EXCLUDE note in boards.py.
    for title in ("Support Engineer", "Technical Support Engineer",
                  "IT Support Specialist", "Help Desk Analyst",
                  "Customer Success Engineer"):
        assert boards.is_fit({"title": title}), f"{title!r} must stay a fit"


def test_salesforce_title_not_killed_by_sales_exclude():
    assert boards.is_fit({"title": "Salesforce Support Specialist"})


# 3) Geo: "<Place> Only" for a non-US/CA place is a hard exclusion.
def test_mexico_only_excluded():
    assert not boards.is_geo_eligible(
        {"title": "Customer Support Representative", "location": "Remote - Mexico Only"})


def test_ontario_only_excluded_us_only_kept():
    assert not boards.is_geo_eligible(
        {"title": "Customer Care Agent", "location": "Remote, Ontario Only"})
    assert not boards.is_geo_eligible(
        {"title": "Support Agent", "location": "Remote",
         "description": "This role is open to candidates residing in Mexico only."})
    assert boards.is_geo_eligible(
        {"title": "Customer Support Specialist", "location": "Remote - US Only"})
    assert boards.is_geo_eligible(
        {"title": "Customer Support Specialist", "location": "Remote (US & Canada)"})


def test_multi_country_only_list_with_us_anchor_kept():
    # US/Canada named just BEFORE "<place> only": the "<place> ... only" regex
    # alternative starts AT the place, so anchors must be tested in a window around
    # the match, not in the match itself (these were wrongly excluded).
    for desc in ("Open to candidates in the US, Canada, and Mexico only.",
                 "This role is open in the United States and Mexico only.",
                 "Eligible countries: USA, UK, Ireland only.",
                 "We serve customers not only in Europe but also the US and Canada."):
        assert boards.is_geo_eligible(
            {"title": "Customer Support Specialist", "location": "Remote",
             "description": desc}), f"{desc!r} is winnable, must be kept"


def test_place_only_still_excluded_when_us_mention_is_out_of_window():
    # A US mention far from the "<place> only" phrase does not rescue it — the
    # restriction itself names no US/CA, so the posting stays unwinnable.
    filler = "Great benefits await you here at our company. " * 3
    assert not boards.is_geo_eligible(
        {"title": "Support Agent", "location": "Remote",
         "description": ("We are proudly US-based. " + filler +
                         "This role is open to candidates residing in Mexico only.")})


# 3b) Language: hard fluency requirements outside the profile's languages.
def test_hard_language_requirement_excluded_unless_spoken():
    job = {"title": "Customer Support Representative",
           "description": "Must be fluent in Spanish and English. Zendesk experience."}
    assert not boards.is_language_eligible(job)  # default: English only
    assert boards.is_language_eligible(job, languages=["English", "Spanish"])


def test_language_nice_to_have_not_excluded():
    job = {"title": "Customer Support Representative",
           "description": "English required; Spanish fluency is a plus."}
    assert boards.is_language_eligible(job)


# 4) Ranking: best-first order; empty-description jobs get score=None and rank last
#    but are NOT dropped; the MIN_SCORE floor (0 by default now — per-vacancy tailoring
#    means we no longer pre-drop low scorers; restorable to 25) drops sub-floor junk.
#    Written to be floor-value-agnostic so it holds whether MIN_SCORE is 0 or 25.
def test_score_floor_ordering_with_none_score_last():
    good = {"title": "Customer Support Specialist", "apply_url": "https://x/1",
            "description": ("Handle Tier 1 and Tier 2 troubleshooting over email, live "
                            "chat, and phone using Zendesk and Intercom. Escalations, "
                            "knowledge base, SLA and CSAT focus. Fully remote.")}
    junk = {"title": "Chat", "apply_url": "https://x/2",
            "description": ("Build our Android application in Kotlin with Jetpack "
                            "Compose, gradle, coroutines and mobile architecture.")}
    empty = {"title": "Customer Support Agent", "apply_url": "https://x/3",
             "description": ""}

    ranked = boards.rank_jobs([junk, empty, good])
    urls = [j["apply_url"] for j in ranked]

    # good (high score) ranks first; empty-description (score None) is kept but ranks last.
    assert urls[0] == "https://x/1", f"good should rank first: {urls}"
    assert urls[-1] == "https://x/3", f"empty-desc should rank last, not be dropped: {urls}"
    assert ranked[0]["score"] >= boards.MIN_SCORE
    assert ranked[-1]["score"] is None  # unknown, kept, ranked last
    # junk is annotated with a score; kept iff it clears the floor, dropped otherwise.
    assert junk["score"] is not None
    if junk["score"] >= boards.MIN_SCORE:
        assert "https://x/2" in urls, f"junk clears floor {boards.MIN_SCORE}, must be kept: {urls}"
    else:
        assert "https://x/2" not in urls, f"junk below floor {boards.MIN_SCORE}, must be dropped: {urls}"


def test_score_job_empty_description_is_none_not_zero():
    assert boards.score_job({"title": "Support Agent", "description": ""}) is None
    assert boards.score_job({"title": "Support Agent"}) is None


# 5) Slug gates: aggregator junk + employer_intel "not hiring remote CS now".
def test_aggregator_slugs_blocked():
    assert "nogigiddy" in boards.AGGREGATOR_EXCLUDE
    assert "nogigiddy" in boards.blocked_slugs()


def test_employer_intel_no_entries_blocked():
    if not os.path.exists(boards._INTEL_PATH):  # data file is repo-local
        import pytest
        pytest.skip("employer_intel.json not present")
    # Boldr's intel entry says hiring_remote_cs_now == "no" (workable slug boldr-1).
    assert "boldr-1" in boards.blocked_slugs()


def test_url_slug_extraction():
    assert boards._url_slug("https://apply.workable.com/boldr-1/") == "boldr-1"
    assert boards._url_slug("https://boards.greenhouse.io/gitlab/jobs/123") == "gitlab"
    assert boards._url_slug("https://example.com/careers") == ""
