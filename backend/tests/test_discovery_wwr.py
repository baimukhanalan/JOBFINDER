"""Offline tests for the We Work Remotely discovery source + the write-time slug
hygiene filter (backend/applier/discovery.py).

Run: PYTHONPATH=. python3 -m pytest backend/tests/test_discovery_wwr.py -q

Pure-logic: the RSS parse is fed fixture XML; the write-filter test mocks the
aggregator fetch so NO network/probe runs.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.applier import discovery  # noqa: E402


_FEED_A = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
  <title>We Work Remotely</title>
  <item>
    <media:content url="https://x/logo.gif" type="image/png"/>
    <title>Acme Corp: Senior Support Engineer</title>
    <guid>https://weworkremotely.com/remote-jobs/acme-corp-senior-support-engineer</guid>
    <country></country>
    <region>Anywhere in the World</region>
  </item>
  <item>
    <title>Globex: Backend Engineer</title>
    <guid>https://weworkremotely.com/remote-jobs/globex-backend-engineer</guid>
    <country>&#127482;&#127480; United States of America</country>
    <region>Anywhere in the World</region>
  </item>
  <item>
    <title>Initech Software: Product Designer @ Initech</title>
    <guid>https://weworkremotely.com/remote-jobs/initech-software-product-designer</guid>
    <country></country>
  </item>
  <item>
    <title>No Colon Title Here</title>
    <guid>https://weworkremotely.com/remote-jobs/no-colon</guid>
    <country></country>
  </item>
</channel>
</rss>"""

# Second feed shares Acme's guid (the master feed repeats every category) under a
# DIFFERENT company label — dedup must suppress it — plus one fresh worldwide job.
_FEED_B = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <item>
    <title>Ghost Company: Role</title>
    <guid>https://weworkremotely.com/remote-jobs/acme-corp-senior-support-engineer</guid>
    <country></country>
  </item>
  <item>
    <title>Fresh Startup: Support Rep</title>
    <guid>https://weworkremotely.com/remote-jobs/fresh-startup-support-rep</guid>
    <country></country>
  </item>
</channel>
</rss>"""


def test_wwr_parse_names_and_country_prefilter():
    seen = set()
    names = discovery._parse_wwr_feed(_FEED_A, seen)
    # company = title before the first colon; region-pinned (<country> non-empty) dropped;
    # a colon-less title is not a "Company: Role" and is skipped.
    assert names == {"Acme Corp", "Initech Software"}
    assert "Globex" not in names  # named country = region-restricted, pre-filtered out


def test_wwr_country_prefilter_can_be_disabled():
    seen = set()
    names = discovery._parse_wwr_feed(_FEED_A, seen, worldwide_only=False)
    # Without the coarse pre-filter, the region-pinned company is kept too.
    assert "Globex" in names
    assert {"Acme Corp", "Initech Software"} <= names


def test_wwr_guid_dedup_across_feeds():
    seen = set()
    a = discovery._parse_wwr_feed(_FEED_A, seen)
    b = discovery._parse_wwr_feed(_FEED_B, seen)  # same seen set = cross-feed dedup
    # Ghost Company reuses Acme's already-seen guid -> suppressed; only the fresh job lands.
    assert b == {"Fresh Startup"}
    assert "Ghost Company" not in (a | b)


def test_write_time_blocklist_filter(tmp_path, monkeypatch):
    """A known-junk aggregator slug (nogigiddy) must never be written, even if it
    arrives as a directly-mined slug; a clean slug alongside it survives."""
    disc = tmp_path / "discovered_slugs.json"
    probed = tmp_path / "discovery_probed_names.json"
    monkeypatch.setattr(discovery, "_DISCOVERED_PATH", str(disc))
    monkeypatch.setattr(discovery, "_PROBED_NAMES_PATH", str(probed))

    async def _fake_aggregator(_client):
        # No names -> no probing/network; direct slugs include the blocked junk one.
        return set(), {"greenhouse": set(), "lever": set(), "ashby": set(),
                       "workable": {"nogigiddy", "realco123"}}

    monkeypatch.setattr(discovery, "_aggregator_companies", _fake_aggregator)

    stats = asyncio.run(discovery.refresh_discovered_slugs())

    written = json.loads(disc.read_text())
    assert "nogigiddy" not in written.get("workable", [])  # blocked at write time
    assert "realco123" in written.get("workable", [])       # clean slug survives
    assert stats["probes"] == 0                              # no name -> no probe
    assert "nogigiddy" in discovery.blocked_slugs()


def test_probed_names_cache_skips_reprobe(tmp_path, monkeypatch):
    """A company name already in the probed-names cache is not re-probed."""
    disc = tmp_path / "discovered_slugs.json"
    probed = tmp_path / "discovery_probed_names.json"
    probed.write_text(json.dumps(["Already Seen Co"]))
    monkeypatch.setattr(discovery, "_DISCOVERED_PATH", str(disc))
    monkeypatch.setattr(discovery, "_PROBED_NAMES_PATH", str(probed))

    async def _fake_aggregator(_client):
        return {"Already Seen Co"}, {a: set() for a in discovery._FETCHERS}

    async def _fail_probe(*_a, **_k):  # must never be called for a cached name
        raise AssertionError("cached name was re-probed")

    monkeypatch.setattr(discovery, "_aggregator_companies", _fake_aggregator)
    monkeypatch.setattr(discovery, "_probe", _fail_probe)

    stats = asyncio.run(discovery.refresh_discovered_slugs())
    assert stats["probes"] == 0
    assert stats["names_skipped_cached"] == 1
